"""OpenWebUI conversation agent."""

from __future__ import annotations
from typing import Literal

from hassil import recognize
from hassil.intents import Intents

from homeassistant.components import assist_pipeline, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid

from markdown_it import MarkdownIt
from mdit_plain.renderer import RendererPlain

from .api import OpenWebUIApiClient
from .const import (
    LOGGER,
    DO_SEARCH_INTENT,
    CONF_BASE_URL,
    CONF_API_KEY,
    CONF_TIMEOUT,
    CONF_MODEL,
    CONF_LANGUAGE_CODE,
    CONF_SEARCH_ENABLED,
    CONF_SEARCH_SENTENCES,
    CONF_SEARCH_RESULT_PREFIX,
    CONF_STRIP_MARKDOWN,
    CONF_VERIFY_SSL,
    DEFAULT_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_LANGUAGE_CODE,
    DEFAULT_SEARCH_ENABLED,
    DEFAULT_SEARCH_SENTENCES,
    DEFAULT_SEARCH_RESULT_PREFIX,
    DEFAULT_STRIP_MARKDOWN,
    DEFAULT_VERIFY_SSL,
)
from .exceptions import ApiCommError, ApiJsonError, ApiTimeoutError
from .message import Message


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> bool:
    """Set up OpenWebUI Conversation Agent from a config entry."""
    agent = OpenWebUIAgent(hass, entry)
    async_add_entities([agent])
    return True


class OpenWebUIAgent(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """OpenWebUI conversation agent."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.hass = hass
        self.entry = entry
        self.timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        self.client = OpenWebUIApiClient(
            base_url=entry.data[CONF_BASE_URL],
            api_key=entry.data[CONF_API_KEY],
            timeout=self.timeout,
            session=async_get_clientsession(hass),
            verify_ssl=entry.options.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        )
        self.history: dict[str, list[Message]] = {}
        self.search_enabled = entry.options.get(CONF_SEARCH_ENABLED, DEFAULT_SEARCH_ENABLED)
        self.search_sentences = [
            x for x in entry.options.get(CONF_SEARCH_SENTENCES, DEFAULT_SEARCH_SENTENCES).splitlines()
            if x.strip()
        ]
        self.search_result_prefix = entry.options.get(CONF_SEARCH_RESULT_PREFIX, DEFAULT_SEARCH_RESULT_PREFIX)
        self.lang = entry.options.get(CONF_LANGUAGE_CODE, DEFAULT_LANGUAGE_CODE).strip()
        self._attr_name = entry.title
        self._attr_unique_id = entry.entry_id
        self.strip_markdown = entry.options.get(CONF_STRIP_MARKDOWN, DEFAULT_STRIP_MARKDOWN)
        self.markdown_parser = MarkdownIt(renderer_cls=RendererPlain)

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()

        # Delay registration until entity_id is available
        async def _register():
            assist_pipeline.async_migrate_engine(
                self.hass, "conversation", self.entry.entry_id, self.entity_id
            )
            conversation.async_set_agent(self.hass, self.entry, self)

        self.hass.async_create_task(_register())

        self.entry.async_on_unload(
            self.entry.add_update_listener(self._async_entry_update_listener)
        )

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a sentence."""
        if user_input.conversation_id in self.history:
            conversation_id = user_input.conversation_id
            conversation_history = self.history[conversation_id]
        else:
            conversation_id = ulid.ulid()
            conversation_history = []

        user_message = Message("user", user_input.text)
        prompt = user_message.message

        should_search = False

        if self.search_enabled and len(self.search_sentences):
            i = Intents.from_dict({
                "language": self.lang,
                "settings": {"ignore_whitespace": True},
                "intents": {
                    DO_SEARCH_INTENT: {"data": [{"sentences": self.search_sentences}]}
                },
                "lists": {"query": {"wildcard": True}},
            })
            r = recognize(prompt, i)
            if r and r.intent.name == DO_SEARCH_INTENT and r.entities.get("query"):
                prompt = r.entities["query"].value
                should_search = True

        try:
            response = await self.query(prompt, conversation_history, should_search)
        except (ApiCommError, ApiJsonError, ApiTimeoutError) as err:
            LOGGER.error("Error generating prompt: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN, f"Something went wrong, {err}"
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )
        except HomeAssistantError as err:
            LOGGER.error("Something went wrong: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "Something went wrong, please check the logs for more information.",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )

        response_data = response["choices"][0]["message"]["content"]
        if self.strip_markdown:
            response_data = self.markdown_parser.render(response_data)
        if should_search:
            response_data = f"{self.search_result_prefix} {response_data}"

        response_message = Message("assistant", response_data)
        conversation_history.extend([user_message, response_message])
        self.history[conversation_id] = conversation_history

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response_data)

        if response_data.strip().endswith("?"):
            LOGGER.debug("💬 Ending in question — enabling expect_response.")
            intent_response.expect_response = True
        else:
            LOGGER.debug("💬 Not a question — follow-up not triggered.")

        LOGGER.debug("🔁 Returning intent_response: %s", intent_response)

        return conversation.ConversationResult(
            response=intent_response, conversation_id=conversation_id
        )

    async def query(self, prompt: str, history: list[Message], search: bool) -> any:
        """Send the prompt to OpenWebUI and return the response."""
        model = self.entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        LOGGER.debug("Prompt for %s: %s", model, prompt)

        system_prompt = (
            "Your name is Jarvis but you are sometimes referred to as Big Detective. "
            "Never include any non-verbal expressions, implied emotions, sound effects, body language, or dramatic flourishes. "
            "Only provide plain, helpful responses relevant to the query. For context, we live in Hawaii. Add a little silly flair. Have some fun! "
            "You can ask additional questions of the user if you want."
        )

        message_list = [{"role": "system", "content": system_prompt}]
        message_list += [{"role": x.role, "content": x.message} for x in history]
        message_list.append({"role": "user", "content": prompt})

        result = await self.client.async_generate(
            {
                "features": {"web_search": search},
                "model": model,
                "messages": message_list,
                "params": {"keep_alive": "-1m"},
                "stream": False,
            }
        )

        LOGGER.debug("Response %s", result["choices"][0]["message"]["content"])
        return result

    async def _async_entry_update_listener(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Handle options update."""
        await hass.config_entries.async_reload(entry.entry_id)
