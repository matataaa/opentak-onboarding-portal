from app.settings import OTS_USERNAME, OTS_PASSWORD, OTS_URL, OTS_VERIFY_SSL
import requests
from .exceptions import AuthenticationError, AuthorizationError, BadRequestError, CSRFError

class OTSClient:

    _apibase = "/api"
    _login = _apibase + "/login"
    _me =  _apibase + "/me"
    _status = _apibase + "/status"
    _certificates = _apibase + "/certificate"
    _data_packages = _apibase + "/data_packages"
    _data_packages_download = _data_packages + "/download"
    _user = _apibase + "/user"
    _users = _apibase + "/users"
    _user_add = _user + "/add"
    _user_delete = _user + "/delete"
    _user_activate = _user + "/activate"
    _user_deactivate = _user + "/deactivate"
    _eud = _apibase + "/eud"
    _jobs = _apibase + "/scheduler/jobs"
    _points = _apibase + "/point"
    _reset = _user + "/password/reset"
    _atak_qr_string = _apibase + "/atak_qr_string"
    _groups = _apibase + "/groups"
    _groups_members = _groups + "/members"
    _meshtastic = _apibase + "/meshtastic"
    _missions = _apibase + "/missions"
    _profiles = _apibase + "/profiles"


    def __init__(self, url, username, password):
        self.base_url = url
        self.username = username
        self.password = password
        self.csrf_token = None
        self.auth_token = None
        self.headers = {"Content-Type": "application/json"}
        self.session = requests.Session()

    
    
    def execute_request(self, method, endpoint, body=None, params=None, response_type="json"):
        try:
            url = self.base_url + endpoint
            # Send the auth token as a header rather than a query parameter so it
            # does not end up in access logs, proxies or the Referer.
            headers = dict(self.headers)
            headers["Referer"] = self.base_url + self._data_packages
            if self.auth_token:
                headers["Authentication-Token"] = self.auth_token
            response = self.session.request(method, url, json=body, headers=headers, params=params, verify=OTS_VERIFY_SSL, timeout=30)
            if response_type == "json":

                if not response.text.strip():
                    return {"response": {}, "status_code": response.status_code }
                try:
                    parsed_body = response.json()
                    return {"response": parsed_body, "status_code": response.status_code }
                except ValueError as e:
                    print(f"Error Parsing Response: {e}")
                    raise ConnectionError(f"Invalid response from OTS server: {e}")
            else:
                return response
            
        except requests.exceptions.RequestException as e:
            print(f"Connection Error: {e}")
            raise ConnectionError(f"Could not connect to OTS server: {e}")

    def request_handler(self, method, endpoint, body=None, params={}, response_type="json"):

   #     if not self.csrf_token:
   #         self.get_csrf_token()

        if not self.auth_token:
            self.login()

        # execute_request attaches the auth token as a header.
        response = self.execute_request(method, endpoint, body, params, response_type)

        if response.get('status_code') == 400: 
            raise BadRequestError(response)

        if response.get('status_code') == 401: 
            raise AuthorizationError(response)
        
        if not response.get('status_code') == 200:
            raise Exception(response)

        return response 

    def get_all_pages(self, fetch, per_page=100, **filters):
        """Call a paginated list method (e.g. get_groups) for every page and merge the results"""
        page = 1
        response = fetch(page=page, per_page=per_page, **filters)
        body = response.get('response', {})
        results = list(body.get('results', []))
        while page < body.get('total_pages', 1):
            page += 1
            next_body = fetch(page=page, per_page=per_page, **filters).get('response', {})
            results.extend(next_body.get('results', []))
        body['results'] = results
        return response


    def get_csrf_token(self):
        response = self.execute_request(method="GET", endpoint=self._login)

        if not response.get('status_code') == 200:
            raise CSRFError(response) 

        self.csrf_token = response.get("response").get("response").get("csrf_token")

        if not self.csrf_token:
            raise Exception(f"Error: Could not get CSRF Token from response: {response}")

    
        self.headers["XSRF-TOKEN"] = self.csrf_token
        self.headers["X-Xsrf-Token"] = self.csrf_token
        return self.csrf_token   

    def login(self):
        body =  {'username': self.username, 'password': self.password} 

        response = self.execute_request(method="POST", endpoint=self._login, body=body, params={'include_auth_token': ''})

        # Safely get the authentication token from nested response
        auth_token = (response.get('response', {})
                 .get('response', {})
                 .get('user', {})
                 .get('authentication_token'))

        if auth_token:
            self.auth_token = auth_token

        if response.get('status_code') == 400:
            raise AuthenticationError(response)

        if response.get('status_code') == 401: 
            raise AuthorizationError(response)
        
        if not response.get('status_code') == 200:
            raise Exception(response)

        return response
    
    def get_me(self):
        return self.request_handler(method="GET", endpoint=self._me)
    
    def get_status(self):
        return self.request_handler(method="GET", endpoint=self._status)
    
    def get_certificate(self, callsign=None):
        params = {}
        if callsign is not None:
            params["callsign"] = callsign
        return self.request_handler(method="GET", endpoint=self._certificates , params=params)

    def create_certificate(self, username):
        body = {'username': username }
        response = self.request_handler(method="POST", endpoint=self._certificates, body=body)
        return response
    
    def upload_data_packages(self, hash):
        params = {'hash': hash}
        return self.request_handler(method="POST", endpoint=self._data_packages, params=params)
    
    def delete_data_package(self, hash):
        params = {'hash': hash}
        return self.request_handler(method="DELETE", endpoint=self._data_packages, params=params)
    
    def get_data_packages(self, page=None, per_page=None, hash=None, filename=None, creator_uid=None, keywords=None, mime_type=None, size=None, tool=None):
        params = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        if hash is not None:
            params["hash"] = hash
        if filename is not None:
            params["filename"] = filename
        if creator_uid is not None:
            params["creator_uid"] = creator_uid
        if keywords is not None:
            params["keywords"] = keywords
        if mime_type is not None:
            params["mime_type"] = mime_type
        if size is not None:
            params["size"] = size
        if tool is not None:
            params["tool"] = tool

        datapackage = self.request_handler(method="GET", endpoint=self._data_packages, params=params)
        return datapackage 

    def download_data_package(self, hash, download_location="./"):
        params = {'hash': hash}
        response = self.request_handler(method="GET", endpoint=self._data_packages_download, params=params, response_type="file")
        filename = response.headers.get('Content-Disposition').split('=')[1]
        path = download_location + filename
        try:
            with open(path, 'wb') as f:
                f.write(response.content)
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False

    def get_euds(self, page=None, per_page=None):
        params = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        return self.request_handler(method="GET", endpoint=self._eud, params=params)
        
    def get_jobs(self):
        return self.request_handler(method="GET", endpoint=self._jobs)

    def get_points(self, page=None, per_page=None, callsign=None, uid=None):
        params = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        if callsign is not None:
            params["callsign"] = callsign
        if uid is not None:
            params["uid"] = uid
        return self.request_handler(method="GET", endpoint=self._points, params=params)
    
    def get_users(self, page=None, per_page=None, username=None, email=None):
        params = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        if username is not None:
            params["username"] = username
        
        return self.request_handler(method="GET", endpoint=self._users, params=params)

    def create_user(self, username, password, roles = ["user"]):
        body = {'username': username, 'password': password, 'confirm_password': password, 'roles': roles}
        return self.request_handler(method="POST", endpoint=self._user_add, body=body)
    
    def delete_user(self, username):
        body = {'username': username}
        return self.request_handler(method="POST", endpoint=self._user_delete, body=body)
    
    def reset_user_password (self, username, password):
        body = {'username': username, 'new_password': password}
        return self.request_handler(method="POST", endpoint=self._reset, body=body)

    def activate_user(self, username):
        body = {'username': username}
        return self.request_handler(method="POST", endpoint=self._user_activate, body=body)
    
    def deactivate_user(self, username):
        body = {'username': username}
        return self.request_handler(method="POST", endpoint=self._user_deactivate, body=body)


    def get_groups(self, page=None, per_page=None, name=None, active=None):
        """Get all groups from OTS"""
        params = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        if name is not None:
            params["name"] = name
        if active is not None:
            params["active"] = active
        return self.request_handler(method="GET", endpoint=self._groups, params=params)

    def create_group(self, name, description=None):
        """Create a new group in OTS"""
        body = {'name': name}
        if description:
            body['description'] = description
        return self.request_handler(method="POST", endpoint=self._groups, body=body)

    def delete_group(self, group_name):
        """Delete a group from OTS"""
        params = {'group_name': group_name}
        return self.request_handler(method="DELETE", endpoint=self._groups, params=params)

    def get_group_members(self, group_name):
        """Get members of a specific OTS group"""
        params = {'name': group_name}
        return self.request_handler(method="GET", endpoint=self._groups_members, params=params)

    def add_user_to_group(self, username, group_name, direction='IN'):
        """Add a user to an OTS group with a specific direction (IN or OUT)"""
        body = {
            'users': [username],
            'group_name': group_name,
            'direction': direction
        }
        return self.request_handler(method="PUT", endpoint=self._groups, body=body)

    def remove_user_from_group(self, username, group_name, direction='IN'):
        """Remove a user from an OTS group"""
        params = {
            'username': username,
            'group_name': group_name,
            'direction': direction
        }
        return self.request_handler(method="DELETE", endpoint=self._groups_members, params=params)

    def get_atak_qr_string(self, username=None):
        params = {}
        if username is not None:
            params["username"] = username
        return self.request_handler(method="GET", endpoint=self._atak_qr_string, params=params)

    def create_atak_qr_string(self, username=None, exp=None, nbf=None, max_uses=None):
        """
        Create ATAK QR string with optional parameters.

        Args:
            username: The username for the client
            exp: Expiration time in unix epoch seconds
            nbf: Not Before - token not valid until this time (unix epoch seconds)
            max_uses: Maximum number of uses for this token
        """
        body = {}
        if username is not None:
            body["username"] = username
        if exp is not None:
            body["exp"] = exp
        if nbf is not None:
            body["nbf"] = nbf
        if max_uses is not None:
            body["max"] = max_uses
        return self.request_handler(method="POST", endpoint=self._atak_qr_string, body=body)

    def delete_atak_qr_string(self, username):
        """Delete/invalidate an existing ATAK QR token"""
        params = {"username": username}
        return self.request_handler(method="DELETE", endpoint=self._atak_qr_string, params=params)

    # iTAK QR String endpoints
    _itak_qr_string = _apibase + "/itak_qr_string"

    def get_itak_qr_string(self, username=None):
        params = {}
        if username is not None:
            params["username"] = username
        return self.request_handler(method="GET", endpoint=self._itak_qr_string, params=params)

    def create_itak_qr_string(self, username=None, exp=None, nbf=None, max_uses=None):
        """
        Create iTAK QR string with optional parameters.

        Args:
            username: The username for the client
            exp: Expiration time in unix epoch seconds
            nbf: Not Before - token not valid until this time (unix epoch seconds)
            max_uses: Maximum number of uses for this token
        """
        body = {}
        if username is not None:
            body["username"] = username
        if exp is not None:
            body["exp"] = exp
        if nbf is not None:
            body["nbf"] = nbf
        if max_uses is not None:
            body["max"] = max_uses
        return self.request_handler(method="POST", endpoint=self._itak_qr_string, body=body)

    # Meshtastic endpoints - uses /api/meshtastic/channel
    _meshtastic_channel = _meshtastic + "/channel"

    def get_meshtastic_channels(self, page=None, per_page=None, name=None, url=None):
        """Get all Meshtastic channels from OTS"""
        params = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        if name is not None:
            params["name"] = name
        if url is not None:
            params["url"] = url
        return self.request_handler(method="GET", endpoint=self._meshtastic_channel, params=params)

    def create_meshtastic_channel(self, url, name=None):
        """Create a new Meshtastic channel in OTS using the meshtastic:// URL"""
        body = {'url': url}
        if name is not None:
            body['name'] = name
        return self.request_handler(method="POST", endpoint=self._meshtastic_channel, body=body)

    def delete_meshtastic_channel(self, url):
        """Delete a Meshtastic channel from OTS by URL"""
        params = {'url': url}
        return self.request_handler(method="DELETE", endpoint=self._meshtastic_channel, params=params)

    def generate_meshtastic_psk(self):
        """Generate a new PSK for Meshtastic channel encryption"""
        endpoint = self._meshtastic + "/generate_psk"
        return self.request_handler(method="GET", endpoint=endpoint)

    def get_missions(self, page=None, per_page=None, name=None, guid=None, tool=None):
        """Get missions from OTS, optionally filtered by name, guid or tool"""
        params = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        if name is not None:
            params["name"] = name
        if guid is not None:
            params["guid"] = guid
        if tool is not None:
            params["tool"] = tool
        return self.request_handler(method="GET", endpoint=self._missions, params=params)

    def create_mission(self, name, creator_uid, description=None, groups=None, default_role=None, tool=None):
        """Create or update a mission in OTS. creator_uid must be an existing EUD UID.

        groups is a list of OTS group IDs the mission is associated with.
        """
        body = {'name': name, 'creator_uid': creator_uid}
        if description is not None:
            body['description'] = description
        if groups is not None:
            body['groups'] = list(groups)
        if default_role is not None:
            body['default_role'] = default_role
        if tool is not None:
            body['tool'] = tool
        return self.request_handler(method="POST", endpoint=self._missions, body=body)

    def delete_mission(self, name):
        """Delete a mission from OTS by name"""
        return self.request_handler(method="DELETE", endpoint=self._missions, params={'name': name})

    def get_device_profiles(self, page=None, per_page=None, preference_key=None, preference_value=None, tool=None):
        """Get device profiles from OTS, optionally filtered by exact field values"""
        params = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        if preference_key is not None:
            params["preference_key"] = preference_key
        if preference_value is not None:
            params["preference_value"] = preference_value
        if tool is not None:
            params["tool"] = tool
        return self.request_handler(method="GET", endpoint=self._profiles, params=params)

    def create_device_profile(self, preference_key, preference_value, value_class,
                              enrollment=False, connection=True, active=True, tool=None, eud_uid=None):
        """Create or update a device profile in OTS.

        OTS validates this endpoint with a WTForm, so it expects form-encoded
        data rather than JSON and cannot go through request_handler.
        """
        if not self.auth_token:
            self.login()

        data = {
            'preference_key': preference_key,
            'preference_value': preference_value,
            'value_class': value_class,
            'enrollment': 'true' if enrollment else 'false',
            'connection': 'true' if connection else 'false',
            'active': 'true' if active else 'false',
        }
        if tool is not None:
            data['tool'] = tool
        if eud_uid is not None:
            data['eud_uid'] = eud_uid

        url = self.base_url + self._profiles
        headers = {
            "Referer": self.base_url + self._data_packages,
            "Authentication-Token": self.auth_token,
        }
        response = self.session.post(url, data=data, headers=headers,
                                     verify=OTS_VERIFY_SSL, timeout=30)

        try:
            payload = response.json() if response.text.strip() else {}
        except ValueError:
            payload = {"response": response.text}
        result = {"response": payload, "status_code": response.status_code}

        if response.status_code == 400:
            raise BadRequestError(result)
        if response.status_code == 401:
            raise AuthorizationError(result)
        if response.status_code != 200:
            raise Exception(result)
        return result

otsClient = OTSClient(OTS_URL, OTS_USERNAME, OTS_PASSWORD)