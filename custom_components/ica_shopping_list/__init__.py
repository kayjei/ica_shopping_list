"""Support to manage a shopping list."""
import asyncio
import logging
import json
import secrets

import voluptuous as vol
from homeassistant.core import HomeAssistant

from homeassistant.core import callback
from homeassistant.components import http
from homeassistant.components.http.data_validator import RequestDataValidator
from homeassistant.helpers import intent
import homeassistant.helpers.config_validation as cv
from homeassistant.util.json import load_json
from homeassistant.helpers.json import save_json
from homeassistant.components import websocket_api
from homeassistant.const import (CONF_PASSWORD, CONF_USERNAME)

import aiohttp #handle http requests

ATTR_NAME = "name"

DOMAIN = "ica_shopping_list"
_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = vol.Schema({
  DOMAIN: {
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
  },
}, extra=vol.ALLOW_EXTRA)

icaUser = None
icaPassword = None
icaList = None
icaStoreSort = None #default store sorting

EVENT_LIST_UPDATED = "shopping_list_updated"

INTENT_ADD_ITEM = "HassShoppingListAddItem"
INTENT_LAST_ITEMS = "HassShoppingListLastItems"
ITEM_UPDATE_SCHEMA = vol.Schema({"complete": bool, ATTR_NAME: str})
PERSISTENCE = ".shopping_list.json"

SERVICE_ADD_ITEM = "add_item"
SERVICE_COMPLETE_ITEM = "complete_item"
SERVICE_CLEAR_ITEM = "clear_item"

SERVICE_ITEM_SCHEMA = vol.Schema({vol.Required(ATTR_NAME): vol.Any(None, cv.string)})

WS_TYPE_SHOPPING_LIST_ITEMS = "shopping_list/items"
WS_TYPE_SHOPPING_LIST_ADD_ITEM = "shopping_list/items/add"
WS_TYPE_SHOPPING_LIST_UPDATE_ITEM = "shopping_list/items/update"
WS_TYPE_SHOPPING_LIST_CLEAR_ITEMS = "shopping_list/items/clear"

SCHEMA_WEBSOCKET_ITEMS = websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
    {vol.Required("type"): WS_TYPE_SHOPPING_LIST_ITEMS}
)

SCHEMA_WEBSOCKET_ADD_ITEM = websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
    {vol.Required("type"): WS_TYPE_SHOPPING_LIST_ADD_ITEM, vol.Required("name"): str}
)

SCHEMA_WEBSOCKET_UPDATE_ITEM = websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
    {
        vol.Required("type"): WS_TYPE_SHOPPING_LIST_UPDATE_ITEM,
        vol.Required("item_id"): str,
        vol.Optional("name"): str,
        vol.Optional("complete"): bool,
    }
)

SCHEMA_WEBSOCKET_CLEAR_ITEMS = websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
    {vol.Required("type"): WS_TYPE_SHOPPING_LIST_CLEAR_ITEMS}
)

API_BASE_URL = 'https://handla.api.ica.se'


async def async_setup(hass: HomeAssistant, config):
    """Initialize the shopping list."""
    global icaUser
    icaUser = config["ica_shopping_list"]["username"]
    global icaPassword
    icaPassword = config["ica_shopping_list"]["password"]
    global icaList
    icaList = config["ica_shopping_list"]["listname"]
    #added storesorting
    global icaStoreSort
    icaStoreSort = config["ica_shopping_list"]["storesorting"]

    #debug config/secrets
    #_LOGGER.debug(config)

    async def add_item_service(call):
        """Add an item with `name`."""
        data = hass.data[DOMAIN]
        name = call.data.get(ATTR_NAME)
        if name is not None:
            item_result = await data.async_add(name)

    async def complete_item_service(call):
        """Mark the item provided via `name` as completed."""
        data = hass.data[DOMAIN]
        name = call.data.get(ATTR_NAME)
        if name is None:
            return
        try:
            item = [item for item in data.items if item["name"] == name][0]
        except IndexError:
            _LOGGER.error("Marking item: %s as completed failed; Item cannot be found", name)
        else:
           await data.async_update(item["id"], {"name": name, "complete": True})

    async def clear_item_service(call):
        """Delete the item provided via `name`."""
        data = hass.data[DOMAIN]
        name = call.data.get(ATTR_NAME)
        if name is None:
            return
        try:
            item = [item for item in data.items if item["name"] == name][0]
        except IndexError:
            _LOGGER.error("Removing item failed: %s cannot be found", name)
        else:
           await data.async_clear(item["id"], {"name": name, "complete": True})

    data = hass.data[DOMAIN] = ShoppingData(hass)
    await data.async_load()

    intent.async_register(hass, AddItemIntent())
    intent.async_register(hass, ListTopItemsIntent())

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_ITEM, add_item_service, schema=SERVICE_ITEM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COMPLETE_ITEM, complete_item_service, schema=SERVICE_ITEM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_ITEM, clear_item_service, schema=SERVICE_ITEM_SCHEMA
    )

    hass.http.register_view(ShoppingListView)
    hass.http.register_view(CreateShoppingListItemView)
    hass.http.register_view(UpdateShoppingListItemView)
    hass.http.register_view(ClearCompletedItemsView)

    hass.components.frontend.async_register_built_in_panel(
        "shopping-list", "shopping_list", "mdi:cart"
    )

    hass.components.websocket_api.async_register_command(
        WS_TYPE_SHOPPING_LIST_ITEMS, websocket_handle_items, SCHEMA_WEBSOCKET_ITEMS
    )
    hass.components.websocket_api.async_register_command(
        WS_TYPE_SHOPPING_LIST_ADD_ITEM, websocket_handle_add, SCHEMA_WEBSOCKET_ADD_ITEM
    )
    hass.components.websocket_api.async_register_command(
        WS_TYPE_SHOPPING_LIST_UPDATE_ITEM,
        websocket_handle_update,
        SCHEMA_WEBSOCKET_UPDATE_ITEM,
    )
    hass.components.websocket_api.async_register_command(
        WS_TYPE_SHOPPING_LIST_CLEAR_ITEMS,
        websocket_handle_clear,
        SCHEMA_WEBSOCKET_CLEAR_ITEMS,
    )

    #Connect.authenticate(icaUser, icaPassword)


    return True


class ShoppingData():
    """Class to hold shopping list data."""

    def __init__(self, hass):
        """Initialize the shopping list."""
        self.hass = hass
        self.items = []

    @callback
    async def async_add(self, name):
        """Add a shopping list item."""
        self.items = []

        # TODO: This should be fetched from the API
        articleGroups = {"Välling":9,"Kaffe":9,"Maskindiskmedel":11,"Hushållspapper":11,"Toapapper":11,"Blöjor":11}

        articleGroup = articleGroups.get(name, 12)

        item = json.dumps({"CreatedRows":[{"IsStrikedOver": "false", "ProductName": name, "SourceId": -1, "ArticleGroupId":articleGroup}]})
        _LOGGER.debug("Item: " + str(item))
        URI = "/api/user/offlineshoppinglists"
        api_data = await Connect.post_request(URI, item,"/sync")

        if api_data is not None and "Rows" in api_data:
            _LOGGER.debug("Adding product: " + str(item))
            for row in api_data["Rows"]:
                name = row["ProductName"].capitalize()
                uuid = row["OfflineId"]
                complete = row["IsStrikedOver"]
                source = row["SourceId"]

                item = {"name": name, "id": uuid, "complete": complete, "SourceId": source}
                _LOGGER.debug("Item: " + str(item))
                self.items.append(item)
        else:
            _LOGGER.error("Failed to get data from API, 180, async_add")

        _LOGGER.debug("Items: " + str(self.items))
        return self.items


    @callback
    async def async_update(self, item_id, info):
        """Update a shopping list item."""

        _LOGGER.debug("Info 200: " + str(item_id) +" - "+ str(info))
        self.items = []

        if info.get("complete") == True or info.get("complete") == False:

            # Await the async_add coroutine and store its result in item
            #completed = await self.async_add(info.get("completed"))
            _LOGGER.debug('complete???' + str(info.get("complete")))
            completed = info.get("complete")
            # Now you can serialize item to JSON
            item = json.dumps({"ChangedRows": [{"OfflineId": item_id, "IsStrikedOver": completed, "SourceId": -1}]})
        elif info.get("name"):
            # Await the async_add coroutine and store its result in item
            item_name = await self.async_add(info.get("name"))
            # Now you can serialize item to JSON
            item = json.dumps({"ChangedRows": [{"OfflineId": item_id, "ProductName": item_name, "SourceId": -1}]})
        _LOGGER.debug("Item 214: " + str(item))

        URI = "/api/user/offlineshoppinglists"
        api_data = await Connect.post_request(URI, item,"/sync")

        if api_data is not None and "Rows" in api_data:
            _LOGGER.debug("Updating product: " + str(item))
            for row in api_data["Rows"]:
                name = row["ProductName"].capitalize()
                uuid = row["OfflineId"]
                complete = row["IsStrikedOver"]
                source = row["SourceId"]

                item = {"name": name, "id": uuid, "complete": complete, "SourceId": source}
                _LOGGER.debug("Item: " + str(item))
                self.items.append(item)

        _LOGGER.debug("Items: " + str(self.items))
        return self.items

    async def clear(self, completed_items: list[str]):
        _LOGGER.debug("Items to delete: " + str(completed_items))

        self.items = []
        item = json.dumps({ "DeletedRows": completed_items })
        _LOGGER.debug("Item: " + str(item))

        URI = "/api/user/offlineshoppinglists"
        api_data = await Connect.post_request(URI, item,"/sync")
        _LOGGER.debug("Adding product: " + str(api_data))
        for row in api_data["Rows"]:
            name = row["ProductName"].capitalize()
            uuid = row["OfflineId"]
            complete = row["IsStrikedOver"]
            source = row["SourceId"]

            item = {"name": name, "id": uuid, "complete": complete, "SourceId": source}
            _LOGGER.debug("Item: " + str(item))
            self.items.append(item)

        _LOGGER.debug("Items: " + str(self.items))
        return self.items


    @callback
    async def async_clear(self, item_id, info):
        """Clear completed items."""
        completed_items = []

        for c_item in self.items:
            if c_item["id"] == item_id: # Checking tha the value exists before trying to delete
                completed_items.append(c_item["id"])

        return await self.clear(completed_items)

    @callback
    async def async_clear_completed(self):
        """Clear completed items."""
        completed_items = []

        for c_item in self.items:
            if c_item["complete"] == True:
                completed_items.append(c_item["id"])

        return await self.clear(completed_items)

    async def async_load(self):
        """Load items."""

        async def load():
            """Load the items synchronously."""
            URI = "/api/user/offlineshoppinglists"
            api_data = await Connect.get_request(URI)
            _LOGGER.debug(api_data)

            if api_data is None:
                _LOGGER.error("Failed to load shopping list data")
                return

            _LOGGER.debug("Adding to ica: " + str(api_data))
            for row in api_data["Rows"]:
                name = row["ProductName"].capitalize()
                uuid = row["OfflineId"]
                complete = row["IsStrikedOver"]
                source = row["SourceId"]

                item = {"name": name, "id": uuid, "complete": complete, "SourceId": source}
                _LOGGER.debug("Item: " + str(item))
                self.items.append(item)

            _LOGGER.debug("Items: " + str(self.items))
            return self.items

        self.items = await self.hass.async_add_job(load)

    def save(self):
        """Save the items."""
        save_json(self.hass.config.path(PERSISTENCE), self.items)


class AddItemIntent(intent.IntentHandler):
    """Handle AddItem intents."""

    intent_type = INTENT_ADD_ITEM
    slot_schema = {"item": cv.string}

    async def async_handle(self, intent_obj):
        """Handle the intent."""
        slots = self.async_validate_slots(intent_obj.slots)
        item = slots["item"]["value"]

        # Await the async_add method to get the result
        result = await intent_obj.hass.data[DOMAIN].async_add(item)

        response = intent_obj.create_response()
        item_result = await intent_obj.hass.data[DOMAIN].async_add(item)
        response.async_set_speech(f"I've added {item_result} to your shopping list")

        intent_obj.hass.bus.async_fire(EVENT_LIST_UPDATED)

        # Return the result
        return response


class ListTopItemsIntent(intent.IntentHandler):
    """Handle AddItem intents."""

    intent_type = INTENT_LAST_ITEMS
    slot_schema = {"item": cv.string}

    async def async_handle(self, intent_obj):
        """Handle the intent."""
        items = intent_obj.hass.data[DOMAIN].items[-5:]
        response = intent_obj.create_response()

        if not items:
            response.async_set_speech("There are no items on your shopping list")
        else:
            response.async_set_speech(
                "These are the top {} items on your shopping list: {}".format(
                    min(len(items), 5),
                    ", ".join(itm["name"] for itm in reversed(items)),
                )
            )
        return response


class ShoppingListView(http.HomeAssistantView):
    """View to retrieve shopping list content."""

    url = "/api/shopping_list"
    name = "api:shopping_list"

    @callback
    def get(self, request):
        """Retrieve shopping list items."""
        return self.json(request.app["hass"].data[DOMAIN].items)


class UpdateShoppingListItemView(http.HomeAssistantView):
    """View to retrieve shopping list content."""

    url = "/api/shopping_list/item/{item_id}"
    name = "api:shopping_list:item:id"

    async def post(self, request, item_id):
        """Update a shopping list item."""
        data = await request.json()

        try:
            item = request.app["hass"].data[DOMAIN].async_update(item_id, data)
            request.app["hass"].bus.async_fire(EVENT_LIST_UPDATED)
            return self.json(item)
        except KeyError:
            return self.json_message("Item not found", 404)
        except vol.Invalid:
            return self.json_message("Item not found", 400)


class CreateShoppingListItemView(http.HomeAssistantView):
    """View to retrieve shopping list content."""

    url = "/api/shopping_list/item"
    name = "api:shopping_list:item"

    @RequestDataValidator(vol.Schema({vol.Required("name"): str}))
    async def post(self, request, data):
        """Create a new shopping list item."""
        item = await request.app["hass"].data[DOMAIN].async_add(data["name"])
        request.app["hass"].bus.async_fire(EVENT_LIST_UPDATED)
        return self.json(item)


class ClearCompletedItemsView(http.HomeAssistantView):
    """View to retrieve shopping list content."""

    url = "/api/shopping_list/clear_completed"
    name = "api:shopping_list:clear_completed"

    @callback
    def post(self, request):
        """Retrieve if API is running."""
        hass = request.app["hass"]
        hass.data[DOMAIN].async_clear_completed()
        hass.bus.async_fire(EVENT_LIST_UPDATED)
        return self.json_message("Cleared completed items.")


@callback
def websocket_handle_items(hass, connection, msg):
    """Handle get shopping_list items."""
    connection.send_message(
        websocket_api.result_message(msg["id"], hass.data[DOMAIN].items)
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "shopping_list/items/add", vol.Required("name"): str}
)
@websocket_api.async_response
async def websocket_handle_add(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, str | int]):
    """Handle add item to shopping_list."""
    item = await hass.data[DOMAIN].async_add(msg["name"])
    hass.bus.async_fire(EVENT_LIST_UPDATED)
    connection.send_message(websocket_api.result_message(msg["id"], item))


@websocket_api.async_response
async def websocket_handle_update(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg):
    """Handle update shopping_list item."""
    msg_id = msg.pop("id")
    item_id = msg.pop("item_id")
    msg.pop("type")
    data = msg

    try:
        item = await hass.data[DOMAIN].async_update(item_id, data)
        hass.bus.async_fire(EVENT_LIST_UPDATED)
    except KeyError:
        connection.send_message(
            websocket_api.error_message(msg_id, "item_not_found", "Item not found")
        )
        return

    connection.send_message(websocket_api.result_message(msg_id, item))


@websocket_api.async_response
async def websocket_handle_clear(hass, connection, msg):
    """Handle clearing shopping_list items."""
    await hass.data[DOMAIN].async_clear_completed()
    hass.bus.async_fire(EVENT_LIST_UPDATED)
    connection.send_message(websocket_api.result_message(msg["id"]))





class Connect:

    AUTHTICKET = None
    listId = None

    def glob_user():
        global icaUser
        return icaUser

    def glob_password():
        global icaPassword
        return icaPassword

    def glob_list():
        global icaList
        return icaList
        
    def glob_icaStoreSort():
        global icaStoreSort
        return icaStoreSort

    @staticmethod
    async def get_request(uri):
        """Do asynchronous API request."""
        if Connect.AUTHTICKET is None:
            renewTicket = await Connect.authenticate()  # Await authentication

            Connect.AUTHTICKET = renewTicket["authTicket"]
            Connect.listId = renewTicket["listId"]

        url = API_BASE_URL + uri + "/" + Connect.listId
        headers = {"Content-Type": "application/json", "AuthenticationTicket": Connect.AUTHTICKET}
        _LOGGER.debug("URL %s", url)

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as response:
                if response.status == 401:
                    # Handle authentication error
                    _LOGGER.debug("API key expired. Acquire new")

                    renewTicket = await Connect.authenticate()  # Await authentication
                    Connect.AUTHTICKET = renewTicket["authTicket"]
                    Connect.listId = renewTicket["listId"]

                elif response.status != 200:
                    _LOGGER.exception("API request returned error,476 %d", response.status)
                else:
                    _LOGGER.debug("API request returned OK %d", response.status)
                    json_data = await response.json()  # Await response content
                    _LOGGER.debug(json_data)
                    return json_data

    @staticmethod
    async def post_request(uri, data, ext):
        """Do asynchronous API request."""
        if Connect.AUTHTICKET is None:
            renewTicket = await Connect.authenticate()  # Await authentication
            Connect.AUTHTICKET = renewTicket["authTicket"]
            Connect.listId = renewTicket["listId"]

        url = API_BASE_URL + uri + "/" + Connect.listId + ext # ext contains "/sync"
        headers = {"Content-Type": "application/json", "AuthenticationTicket": Connect.AUTHTICKET}
        _LOGGER.debug("URL %s", url)

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, data=data) as response:
                if response.status == 401:
                    # Handle authentication error
                    _LOGGER.debug("API key expired. Acquire new")

                    renewTicket = await Connect.authenticate()  # Await authentication
                    Connect.AUTHTICKET = renewTicket["authTicket"]
                    Connect.listId = renewTicket["listId"]

                elif response.status != 200:
                    _LOGGER.exception("API request returned error, 506 %d", response.status)
                else:
                    _LOGGER.debug("API request returned OK %d", response.status)

                    json_data = await response.json()  # Await response content
                    _LOGGER.debug(json_data)
                    return json_data

    @staticmethod
    async def authenticate():
        """Do asynchronous API request"""

        icaUser = Connect.glob_user()
        icaPassword = Connect.glob_password()
        icaList = Connect.glob_list()
        listId = None
        icaStoreSort = Connect.glob_icaStoreSort()

        url = f"{API_BASE_URL}/api/login"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=aiohttp.BasicAuth(icaUser, icaPassword)) as response:
                if response.status != 200:
                    _LOGGER.exception("API request returned error, 526 %d", response.status)
                else:
                    _LOGGER.debug("API request returned OK %d", response.status)
                    authTick = response.headers["AuthenticationTicket"]

                    if Connect.listId is None:
                        url = f"{API_BASE_URL}/api/user/offlineshoppinglists"
                        headers = {"Content-Type": "application/json", "AuthenticationTicket": authTick}

                        async with session.get(url, headers=headers) as response:
                            response = await response.json()
                            for lists in response["ShoppingLists"]:
                                if lists["Title"] == icaList:
                                    listId = lists["OfflineId"]

                        if Connect.listId is None and listId is None:
                            _LOGGER.info("Shopping-list not found: %s", icaList)
                            newOfflineId = secrets.token_hex(4) + "-" + secrets.token_hex(2) + "-" + secrets.token_hex(2) + "-"
                            newOfflineId = newOfflineId + secrets.token_hex(2) + "-" + secrets.token_hex(6)
                            _LOGGER.debug("New hex-string: %s", newOfflineId)

                            icaStoreSort = 0 if icaStoreSort is None else icaStoreSort

                            data = json.dumps({"OfflineId": newOfflineId, "Title": icaList if listId is None else listId, "SortingStore": icaStoreSort})

                            url = f"{API_BASE_URL}/api/user/offlineshoppinglists"
                            headers = {"Content-Type": "application/json", "AuthenticationTicket": authTick}

                            _LOGGER.debug("List does not exist. Creating %s", icaList)

                            async with session.post(url, headers=headers, data=data) as response:
                                if response.status == 200:
                                    url = f"{API_BASE_URL}/api/user/offlineshoppinglists"
                                    headers = {"Content-Type": "application/json", "AuthenticationTicket": authTick}

                                    async with session.get(url, headers=headers) as response:
                                        response = await response.json()

                                        for lists in response["ShoppingLists"]:
                                            if lists["Title"] == icaList:
                                                listId = lists["OfflineId"]
                                                _LOGGER.debug(icaList + " created with offlineId %s", listId)

                    authResult = {"authTicket": authTick, "listId": listId}
                    _LOGGER.debug("authTicket: %s", authTick)
                    _LOGGER.debug("New listId: %s", listId)
                    return authResult
