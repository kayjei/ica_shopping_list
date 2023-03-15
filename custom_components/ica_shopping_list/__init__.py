"""Support to manage a shopping list."""
import asyncio
import logging
import uuid
import requests
import json
import secrets

import voluptuous as vol

#from homeassistant.const import HTTP_NOT_FOUND, HTTP_BAD_REQUEST
from homeassistant.core import callback
from homeassistant.components import http
from homeassistant.components.http.data_validator import RequestDataValidator
from homeassistant.helpers import intent
import homeassistant.helpers.config_validation as cv
from homeassistant.util.json import load_json, save_json
from homeassistant.components import websocket_api
from homeassistant.const import (CONF_PASSWORD, CONF_USERNAME)

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

EVENT = "shopping_list_updated"
INTENT_ADD_ITEM = "HassShoppingListAddItem"
INTENT_LAST_ITEMS = "HassShoppingListLastItems"
ITEM_UPDATE_SCHEMA = vol.Schema({"complete": bool, ATTR_NAME: str})
PERSISTENCE = ".shopping_list.json"

SERVICE_ADD_ITEM = "add_item"
SERVICE_COMPLETE_ITEM = "complete_item"

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


@asyncio.coroutine
def async_setup(hass, config):
    """Initialize the shopping list."""
    global icaUser
    icaUser = config["ica_shopping_list"]["username"]
    global icaPassword
    icaPassword = config["ica_shopping_list"]["password"]
    global icaList
    icaList = config["ica_shopping_list"]["listname"]
    _LOGGER.debug(config)

    @asyncio.coroutine
    def add_item_service(call):
        """Add an item with `name`."""
        data = hass.data[DOMAIN]
        name = call.data.get(ATTR_NAME)
        if name is not None:
            data.async_add(name)

    @asyncio.coroutine
    def complete_item_service(call):
        """Mark the item provided via `name` as completed."""
        data = hass.data[DOMAIN]
        name = call.data.get(ATTR_NAME)
        if name is None:
            return
        try:
            item = [item for item in data.items if item["name"] == name][0]
        except IndexError:
            _LOGGER.error("Removing of item failed: %s cannot be found", name)
        else:
            data.async_update(item["id"], {"name": name, "complete": True})

    data = hass.data[DOMAIN] = ShoppingData(hass)
    yield from data.async_load()

    intent.async_register(hass, AddItemIntent())
    intent.async_register(hass, ListTopItemsIntent())

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_ITEM, add_item_service, schema=SERVICE_ITEM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COMPLETE_ITEM, complete_item_service, schema=SERVICE_ITEM_SCHEMA
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


class ShoppingData:
    """Class to hold shopping list data."""

    def __init__(self, hass):
        """Initialize the shopping list."""
        self.hass = hass
        self.items = []

    @callback
    def async_add(self, name):
        """Add a shopping list item."""
        self.items = []
        item = json.dumps({"CreatedRows":[{"IsStrikedOver": "false", "ProductName": name}]})
        _LOGGER.debug("Item: " + str(item))
        URI = "/api/user/offlineshoppinglists"
        api_data = Connect.post_request(URI, item)
        _LOGGER.debug("Adding product: " + str(item))
        for row in api_data["Rows"]:
            name = row["ProductName"].capitalize()
            uuid = row["OfflineId"]
            complete = row["IsStrikedOver"]

            item = {"name": name, "id": uuid, "complete": complete}
            _LOGGER.debug("Item: " + str(item))
            self.items.append(item)

        _LOGGER.debug("Items: " + str(self.items))
        return self.items


    @callback
    def async_update(self, item_id, info):
        """Update a shopping list item."""

        _LOGGER.debug("Info: " + str(info))
        self.items = []

        if info.get("complete") == True or info.get("complete") == False:
            item = json.dumps({ "ChangedRows": [ { "OfflineId": item_id, "IsStrikedOver": info.get("complete") } ] })
        elif info.get("name"):
            item = json.dumps({ "ChangedRows": [ { "OfflineId": item_id, "ProductName": info.get("name") } ] })
        _LOGGER.debug("Item: " + str(item))

        URI = "/api/user/offlineshoppinglists"
        api_data = Connect.post_request(URI, item)
        _LOGGER.debug("Updating product: " + str(item))
        for row in api_data["Rows"]:
            name = row["ProductName"].capitalize()
            uuid = row["OfflineId"]
            complete = row["IsStrikedOver"]

            item = {"name": name, "id": uuid, "complete": complete}
            _LOGGER.debug("Item: " + str(item))
            self.items.append(item)

        _LOGGER.debug("Items: " + str(self.items))
        return self.items


    @callback
    def async_clear_completed(self):
        """Clear completed items."""
        completed_items = []

        for c_item in self.items:
            if c_item["complete"] == True:
                completed_items.append(c_item["id"])
        _LOGGER.debug("Items to delete: " + str(completed_items))

        self.items = []
        item = json.dumps({ "DeletedRows": completed_items })
        _LOGGER.debug("Item: " + str(item))

        URI = "/api/user/offlineshoppinglists"
        api_data = Connect.post_request(URI, item)
        _LOGGER.debug("Adding product: " + str(api_data))
        for row in api_data["Rows"]:
            name = row["ProductName"].capitalize()
            uuid = row["OfflineId"]
            complete = row["IsStrikedOver"]

            item = {"name": name, "id": uuid, "complete": complete}
            _LOGGER.debug("Item: " + str(item))
            self.items.append(item)

        _LOGGER.debug("Items: " + str(self.items))
        return self.items

    @asyncio.coroutine
    def async_load(self):
        """Load items."""

        def load():
            """Load the items synchronously."""
            URI = "/api/user/offlineshoppinglists"
            api_data = Connect.get_request(URI)
            _LOGGER.debug("Adding to ica: " + str(api_data))
            for row in api_data["Rows"]:
                name = row["ProductName"].capitalize()
                uuid = row["OfflineId"]
                complete = row["IsStrikedOver"]

                item = {"name": name, "id": uuid, "complete": complete}
                _LOGGER.debug("Item: " + str(item))
                self.items.append(item)

            _LOGGER.debug("Items: " + str(self.items))
            return self.items
#            return load_json(self.hass.config.path(PERSISTENCE), default=[])

        self.items = yield from self.hass.async_add_job(load)

    def save(self):
        """Save the items."""
        save_json(self.hass.config.path(PERSISTENCE), self.items)


class AddItemIntent(intent.IntentHandler):
    """Handle AddItem intents."""

    intent_type = INTENT_ADD_ITEM
    slot_schema = {"item": cv.string}

    @asyncio.coroutine
    def async_handle(self, intent_obj):
        """Handle the intent."""
        slots = self.async_validate_slots(intent_obj.slots)
        item = slots["item"]["value"]
        intent_obj.hass.data[DOMAIN].async_add(item)

        response = intent_obj.create_response()
        response.async_set_speech(f"I've added {item} to your shopping list")
        intent_obj.hass.bus.async_fire(EVENT)
        return response


class ListTopItemsIntent(intent.IntentHandler):
    """Handle AddItem intents."""

    intent_type = INTENT_LAST_ITEMS
    slot_schema = {"item": cv.string}

    @asyncio.coroutine
    def async_handle(self, intent_obj):
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
            request.app["hass"].bus.async_fire(EVENT)
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
    @asyncio.coroutine
    def post(self, request, data):
        """Create a new shopping list item."""
        item = request.app["hass"].data[DOMAIN].async_add(data["name"])
        request.app["hass"].bus.async_fire(EVENT)
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
        hass.bus.async_fire(EVENT)
        return self.json_message("Cleared completed items.")


@callback
def websocket_handle_items(hass, connection, msg):
    """Handle get shopping_list items."""
    connection.send_message(
        websocket_api.result_message(msg["id"], hass.data[DOMAIN].items)
    )


@callback
def websocket_handle_add(hass, connection, msg):
    """Handle add item to shopping_list."""
    item = hass.data[DOMAIN].async_add(msg["name"])
    hass.bus.async_fire(EVENT)
    connection.send_message(websocket_api.result_message(msg["id"], item))


@websocket_api.async_response
async def websocket_handle_update(hass, connection, msg):
    """Handle update shopping_list item."""
    msg_id = msg.pop("id")
    item_id = msg.pop("item_id")
    msg.pop("type")
    data = msg

    try:
        item = hass.data[DOMAIN].async_update(item_id, data)
        hass.bus.async_fire(EVENT)
        connection.send_message(websocket_api.result_message(msg_id, item))
    except KeyError:
        connection.send_message(
            websocket_api.error_message(msg_id, "item_not_found", "Item not found")
        )


@callback
def websocket_handle_clear(hass, connection, msg):
    """Handle clearing shopping_list items."""
    hass.data[DOMAIN].async_clear_completed()
    hass.bus.async_fire(EVENT)
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

    @staticmethod
    def get_request(uri):
        """Do API request."""
        if Connect.AUTHTICKET is None:
            renewTicket = Connect.authenticate()
            Connect.AUTHTICKET = renewTicket["authTicket"]
            Connect.listId = renewTicket["listId"]

        url = "https://handla.api.ica.se" + uri + "/" + Connect.listId
        headers = {"Content-Type": "application/json", "AuthenticationTicket": Connect.AUTHTICKET}
        req = requests.get(url, headers=headers)

        if req.status_code == 401:
            _LOGGER.debug("API key expired. Aquire new")

            renewTicket = Connect.authenticate()
            Connect.AUTHTICKET = renewTicket["authTicket"]
            Connect.listId = renewTicket["listId"]
            
            headers = {"Content-Type": "application/json", "AuthenticationTicket": Connect.AUTHTICKET}
            req = requests.get(url, headers=headers)

            if req.status_code != 200:
                _LOGGER.exception("API request returned error %d", req.status_code)

            else:
                _LOGGER.debug("API request returned OK %d", req.text)

                json_data = json.loads(req.content)
                return json_data

        elif req.status_code != 200:
            _LOGGER.exception("API request returned error %d", req.status_code)
        else:
            _LOGGER.debug("API request returned OK %d", req.text)

        json_data = json.loads(req.content)
        return json_data

    @staticmethod
    def post_request(uri, data):
        """Do API request."""
        if Connect.AUTHTICKET is None:
            renewTicket = Connect.authenticate()
            Connect.AUTHTICKET = renewTicket["authTicket"]
            Connect.listId = renewTicket["listId"]

        url = "https://handla.api.ica.se" + uri + "/" + Connect.listId + "/sync"
        _LOGGER.debug("URL: " + url)
        headers = {"Content-Type": "application/json", "AuthenticationTicket": Connect.AUTHTICKET}
        req = requests.post(url, headers=headers, data=data)

        if req.status_code == 401:
            _LOGGER.debug("API key expired. Aquire new")

            renewTicket = Connect.authenticate()
            Connect.AUTHTICKET = renewTicket["authTicket"]
            Connect.listId = renewTicket["listId"]
            
            headers = {"Content-Type": "application/json", "AuthenticationTicket": Connect.AUTHTICKET}
            req = requests.post(url, headers=headers)

            if req.status_code != 200:
                _LOGGER.exception("API request returned error %d", req.status_code)

            else:
                _LOGGER.debug("API request returned OK %d", req.text)

                json_data = json.loads(req.content)
                return json_data

        elif req.status_code != 200:
            _LOGGER.exception("API request returned error %d", req.status_code)
        else:
            _LOGGER.debug("API request returned OK %d", req.text)

        json_data = json.loads(req.content)
        return json_data

    @staticmethod
    def authenticate():
        """Do API request"""

        icaUser = Connect.glob_user()
        icaPassword = Connect.glob_password()
        icaList = Connect.glob_list()
        listId = None

        url = "https://handla.api.ica.se/api/login"
        req = requests.get(url, auth=(str(icaUser), str(icaPassword)))

        if req.status_code != 200:
            _LOGGER.exception("API request returned error %d", req.status_code)
        else:
            _LOGGER.debug("API request returned OK %d", req.text)
            authTick = req.headers["AuthenticationTicket"]

            if Connect.listId is None:
                url = 'https://handla.api.ica.se/api/user/offlineshoppinglists'
                headers = {"Content-Type": "application/json", "AuthenticationTicket": authTick}
                req = requests.get(url, headers=headers)
                response = json.loads(req.content)

                for lists in response["ShoppingLists"]:
                    if lists["Title"] == icaList:
                        listId = lists["OfflineId"]
            
                if Connect.listId is None and listId is None:
                    _LOGGER.info("Shopping-list not found: %s", icaList)
                    newOfflineId = secrets.token_hex(4) + "-" + secrets.token_hex(2) + "-" + secrets.token_hex(2) + "-"
                    newOfflineId = newOfflineId + secrets.token_hex(2) + "-" + secrets.token_hex(6)
                    _LOGGER.debug("New hex-string: %s", newOfflineId)
                    data = json.dumps({"OfflineId": newOfflineId, "Title": icaList, "SortingStore": 0})

                    url = 'https://handla.api.ica.se/api/user/offlineshoppinglists'
                    headers = {"Content-Type": "application/json", "AuthenticationTicket": authTick}
                    
                    _LOGGER.debug("List does not exist. Creating %s", icaList)
                    req = requests.post(url, headers=headers, data=data)

                    if req.status_code == 200:
                        url = 'https://handla.api.ica.se/api/user/offlineshoppinglists'
                        headers = {"Content-Type": "application/json", "AuthenticationTicket": authTick}
                        req = requests.get(url, headers=headers)
                        response = json.loads(req.content)

                        _LOGGER.debug(response)

                        for lists in response["ShoppingLists"]:
                            if lists["Title"] == icaList:
                                listId = lists["OfflineId"]
                                _LOGGER.debug(icaList + " created with offlineId %s", listId)

            authResult = {"authTicket": authTick, "listId": listId}
            return authResult
