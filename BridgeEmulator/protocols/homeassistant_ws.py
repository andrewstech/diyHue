import json
import logging
import time
import random

from ws4py.client.threadedclient import WebSocketClient
from functions import light_types, nextFreeId
from functions.colors import hsv_to_rgb


next_connection_error_log = 0
logging_backoff = 2 # 2 Second back off
homeassistant_token = ''
homeassistant_url = 'ws://127.0.0.1:8123/api/websocket'
ws = None
include_by_default = False

discovered_devices = {}
# This is Home Assistant States so looks like this:
# {
#   'entity_id': 'light.my_light',
#   'state': 'on',
#   'attributes': {
#        'min_mireds': 153, 
#        'max_mireds': 500,
#        'effect_list': ['colorloop', 'random'],
#        'brightness': 254, 
#        'hs_color': [291.687, 65.098], 
#        'rgb_color': [232, 89, 255], 
#        'xy_color': [0.348, 0.168], 
#        'is_hue_group': True, 
#        'friendly_name': 'My Light', 
#        'supported_features': 63
#   },
#   'last_changed': '2019-01-09T10:35:39.148462+00:00',
#    'last_updated': '2019-01-09T10:35:39.148462+00:00', 
#    'context': {'id': 'X', 'parent_id': None, 'user_id': None}
# }
latest_states = {}

class HomeAssistantClient(WebSocketClient):

    message_id = 1
    id_to_type = {}

    def opened(self):
        logging.info("Home Assistant WebSocket Connection Opened")

    def closed(self, code, reason=None):
        logging.info("Home Assistant WebSocket Connection Closed. Code: {} Reason {}".format(code, reason))
        discovered_devices.clear()
        for home_assistant_state in latest_states.values():
            if 'state' in home_assistant_state:
                home_assistant_state['state'] = 'unavailable'

    def received_message(self, m):
        logging.debug("Received message: {}".format(m))
        message_text = m.data.decode(m.encoding)
        message = json.loads(message_text)
        if message.get('type', None) == "auth_required":
            self.do_auth_required(message)
        elif message.get('type', None) == "auth_ok":
            self.do_auth_complete()
        elif message.get('type', None) == "auth_invalid":
            self.do_auth_invalid(message)
        elif message.get('type', None) == "result":
            self.do_result(message)
        elif message.get('type', None) == "event":
            self.do_event(message)
        elif message.get('type', None) == "pong":
            self.do_pong(message)
        else:
            logging.warning("Unexpected message: ", message)

    def do_auth_required(self, m):
        logging.info("Home Assistant Web Socket Authorisation required")
        payload = {
                'type':'auth',
                'access_token': homeassistant_token
        }
        self._send(payload)

    def do_auth_invalid(self, message):
        logging.error("Home Assistant Web Socket Authorisation invalid: {}".format(message))

    def do_auth_complete(self):
        logging.info("Home Assistant Web Socket Authorisation complete")
        self.get_all_lights()
        self.subscribe_for_updates()

    def get_all_lights(self):
        payload = {
            'type' : 'get_states'
        }
        self._send_with_id(payload, "getstates")

    def subscribe_for_updates(self):
        payload = {
            "type": "subscribe_events",
            "event_type": "state_changed"
        }
        self._send_with_id(payload, "subscribe")

    def change_light(self, address, light, data):
        service_data = {}
        service_data['entity_id'] = address['entity_id']

        payload = {
            "type": "call_service",
            "domain": "light",
            "service_data": service_data
        }

        payload["service"] = "turn_off"
        if 'state' in light and 'on' in light['state']:
            if light['state']['on']:
                payload["service"] = "turn_on"

        color_from_hsv = False
        for key, value in data.items():
            if key == "ct":
                service_data['color_temp'] = value
            if key == "bri":
                service_data['brightness'] = value
            if key == "xy":
                service_data['xy_color'] = [value[0], value[1]]
            if key == "hue":
                color_from_hsv = True
            if key == "sat":
                color_from_hsv = True
            if key == "on":
                if value:
                    payload["service"] = "turn_on"
                else:
                    payload["service"] = "turn_off"
            if key == "alert":
                service_data['alert'] = value
            if key == "transitiontime":
                service_data['transition'] = value / 10

        if color_from_hsv:
            color = hsv_to_rgb(data['hue'], data['sat'], light["state"]["bri"])
            service_data['rgb_color'] = { 'r': color[0], 'g': color[1], 'b': color[2] }

        self._send_with_id(payload, "service")


    def do_result(self, message):
        if 'result' in message and message['result']:
            message_type = self.id_to_type.pop(message['id'])
            if message_type == "getstates":
                for x in message['result']:
                    entity_id = x.get('entity_id', None)
                    if entity_id and entity_id.startswith('light.'):
                        if self._should_include(x):
                            logging.info(f"Found {entity_id}")
                            discovered_devices[entity_id] = x
                            latest_states[entity_id] = x

    def do_event(self, message):
        try:
            event_type = message['event']['event_type']
            if event_type == 'state_changed':
                self.do_state_changed(message)
        except KeyError:
            logging.exception("No event_type  in event")

    def do_state_changed(self, message):
        try:
            entity_id = message['event']['data']['entity_id']
            new_state = message['event']['data']['new_state']
            if entity_id.startswith("light."):
                if self._should_include(new_state):
                    logging.debug("State update recevied for {}, new state {}".format(entity_id, new_state))
                    latest_states[entity_id] = new_state
        except KeyError as e:
            logging.exception("No state in event: {}", message)

    def _should_include(self, new_state):
        should_include = False
        diy_hue_flag = None
        if 'attributes' in new_state and 'diyhue' in new_state['attributes']:
            diy_hue_flag = new_state['attributes']['diyhue']

        if include_by_default:
            if diy_hue_flag is not None and diy_hue_flag == "exclude":
                should_include = False
            else:
                should_include = True
        else:
            if diy_hue_flag is not None and diy_hue_flag == "include":
                should_include = True
            else:
                should_include = False
        logging.debug("Home Asssitant Web Socket should include? {} - Include By Default? {}, Attribute: {} - State {}".format(should_include, include_by_default, diy_hue_flag, new_state))
        return should_include


    def _send_with_id(self, payload, type_of_call):
        payload['id'] = self.message_id
        self.id_to_type[self.message_id] = type_of_call
        self.message_id += 1
        self._send(payload)

    def _send(self, payload):
        json_payload = json.dumps(payload)
        self.send(json_payload)        

def connect_if_required():
    if ws is None or ws.client_terminated: 
        create_websocket_client()        

def create_websocket_client():
    global ws
    global next_connection_error_log
    global logging_backoff
    if time.time() >= next_connection_error_log:
        logging.warning("Home Assistant Web Socket Client disconnected trying to (re)connect")

    try:
        ws = HomeAssistantClient(home_assistant_url, protocols=['http-only', 'chat'])
        ws.connect()
        logging.info("Home Assistant Web Socket Client connected")
    except:
        if time.time() >= next_connection_error_log:
            logging.exception("Error connecting to Home Assistant WebSocket")
            next_connection_error_log = time.time() + logging_backoff
            logging_backoff = logging_backoff * 2
        ws = None


def create_ws_client(config, lights, adresses, sensors):
    global homeassistant_token
    global home_assistant_url
    global include_by_default
    if config['homeAssistantIp'] is not None:
        homeassistant_ip = config['homeAssistantIp']
    if config['homeAssistantPort'] is not None:
        homeAssistant_port = config['homeAssistantPort']
    if config['homeAssistantToken'] is not None:
        homeassistant_token = config['homeAssistantToken']
    if config['homeAssistantIncludeByDefault'] is not None:
        include_by_default = config['homeAssistantIncludeByDefault']

    home_assistant_url = f'ws://{homeassistant_ip}:{homeAssistant_port}/api/websocket'
    connect_if_required()


def discover(bridge_config, new_lights):
    logging.info("HomeAssistant WebSocket discovery called")
    connect_if_required()
    ws.get_all_lights()
    # Give the call a chance to return
    time.sleep(15)
    # This only loops over discovered devices so we have already filtered out what we don't want
    for entity_id, data in discovered_devices.items():
        device_new = True
        light_name = data["attributes"]["friendly_name"] if data["attributes"]["friendly_name"] is not None else entity_id

        for lightkey in bridge_config["lights_address"].keys():
            if bridge_config["lights_address"][lightkey]["protocol"] == "homeassistant_ws":
                if bridge_config["lights_address"][lightkey]["entity_id"] == entity_id:
                    device_new = False
                    break
    
        if device_new:
            logging.info("HomeAssistant_ws: Adding light {}".format(light_name))
            new_light_id = nextFreeId(bridge_config, "lights")

            # 'entity_id', 'state', 'attributes', 'last_changed', 'last_updated', 'context'
            # From Home Assistant lights/__init.py__
            SUPPORT_BRIGHTNESS = 1
            SUPPORT_COLOR_TEMP = 2
            SUPPORT_EFFECT = 4
            SUPPORT_FLASH = 8
            SUPPORT_COLOR = 16
            SUPPORT_TRANSITION = 32
            SUPPORT_WHITE_VALUE = 128
            supported_features = data['attributes']['supported_features']

            model_id = None
            if supported_features & SUPPORT_COLOR:
                model_id = "HomeAssistant-RGB"
            elif supported_features & SUPPORT_COLOR_TEMP:
                model_id = "HomeAssistant-WhiteAmbiance"
            elif supported_features & SUPPORT_BRIGHTNESS:
                model_id = "HomeAssistant-Dimmable"
            else:
                model_id = "HomeAssistant-Switch"

            bridge_config["lights"][new_light_id] = {
                "state": light_types[model_id]["state"],
                "type": light_types[model_id]["type"],
                "name": light_name,
                "uniqueid": "4a:e0:ad:7f:cf:" + str(
                    random.randrange(0, 99)) + "-1",
                "modelid": model_id, 
                "manufacturername": "Home Assistant",
                "swversion": light_types[model_id]["swversion"]
            }
            new_lights.update({new_light_id: {"name": light_name}})
            bridge_config["lights_address"][new_light_id] = {
                "protocol": "homeassistant_ws",
                "entity_id": entity_id
            }
    logging.info("HomeAssistant WebSocket discovery complete")

def translate_homeassistant_state_to_diyhue_state(ha_state):
    '''
    Home Assistant:
    {
        "entity_id": "light.my_light", 
        "state": "off", 
        "attributes": {
            "min_mireds": 153, 
            "max_mireds": 500, 
            # If using color temp
            "brightness": 254, "color_temp": 345, 
            # If using colour:
            "brightness": 254, "hs_color": [262.317, 64.314], "rgb_color": [151, 90, 255], "xy_color": [0.243, 0.129]
            "effect_list": ["colorloop", "random"], 
            "friendly_name": "My Light", 
            "supported_features": 63
        }, 
        "last_changed": "2020-12-09T17:46:40.569891+00:00", 
        "last_updated": "2020-12-09T17:46:40.569891+00:00", 
    }    

    Diy Hue:
    "state": {
.        "alert": "select",
        "bri": 249,
        # Either ct, hs or xy 
        # If ct then uses ct
        # If xy uses xy
        # If hs uses hue/sat
        "colormode": "xy",
.        "effect": "none",
        "ct": 454,
        "hue": 0,
        "on": true,
.        "reachable": true,
        "sat": 0,
        "xy": [
            0.478056,
            0.435106
        ]
    },
    '''

    reachable = False
    state = 'off'
    if "state" in ha_state and homeassistant_state['state'] in ['on','off']:
        reachable = True
        state = homeassistant_state['state'] == 'on' 

    diyhue_state = {
        "alert": "none",
        "mode": "homeautomation",
        "effect": "none",
        "reachable": reachable,
        "state": state
    }

    if "attributes" in ha_state:
        for key, value in ha_state['attributes'].items():
            if key == "brightness":
                diyhue_state['bri'] = value
            if key == "color_temp"":
                diyhue_state['ct'] = value
                diyhue_state['colormode'] = 'ct'
            if key == "xy_color":
                diyhue_state['xy'] = [value[0], value[1]]
                diyhue_state['colormode'] = 'xy'

    return state

def set_light(address, light, data):
    connect_if_required()            
    ws.change_light(address, light, data)

def get_light_state(address, light):
    connect_if_required()
    if latest_states[address['entity_id']] is None:
        return { 'reachable': False }

    homeassistant_state = latest_states[address['entity_id']]

    if homeassistant_state['state'] not in ['on','off']:
        return { 'reachable': False }

    state = { 'reachable': True }
    state['on'] = homeassistant_state['state'] == 'on'
    if homeassistant_state['state'] == 'off':
        return state
            
    # If we get here the light is ON
    attributes = homeassistant_state['attributes']
    if attributes:
        if "brightness" in attributes:
            state['bri'] = attributes["brightness"]
        if "color_temp" in attributes:
            state["colormode"] = "ct"
            state['ct'] = attributes["color_temp"]
        if "xy_color" in attributes:
            state["colormode"] = "xy"
            state['xy'] = attributes["xy_color"]
    return state
