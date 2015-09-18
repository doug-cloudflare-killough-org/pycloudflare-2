from property_caching import (
    cached_property, clear_property_cache, set_property_cache)
from six import iteritems, itervalues

from pycloudflare.services import (
    CloudFlareHostService, CloudFlarePageIterator, CloudFlareService)


class User(object):
    def __init__(self, email, api_key):
        self.email = email
        self.service = self.get_service(api_key, email)

    @classmethod
    def get_host_service(cls):
        return CloudFlareHostService()

    @classmethod
    def get_service(cls, api_key, email):
        return CloudFlareService(api_key, email)

    @classmethod
    def get_or_create(cls, email, password, username=None, unique_id=None):
        service = cls.get_host_service()
        data = service.user_create(email, password, username, unique_id)
        return cls.create_from_host_api_response(data)

    @classmethod
    def get(cls, email=None, unique_id=None):
        service = cls.get_host_service()
        data = service.user_lookup(email=email, unique_id=unique_id)
        return cls.create_from_host_api_response(data)

    @classmethod
    def create_from_host_api_response(cls, data):
        user = User(data['cloudflare_email'], data['user_api_key'])
        set_property_cache(user, '_host_api_data', data)
        return user

    @cached_property
    def _host_api_data(self):
        service = self.get_host_service()
        return service.user_lookup(email=self.email)

    @property
    def user_key(self):
        return self._host_api_data['user_key']

    @cached_property
    def zones(self):
        return list(self.iter_zones())

    def iter_zones(self):
        for zone in CloudFlarePageIterator(self.service.get_zones):
            yield Zone(self, zone)

    def get_zone_by_name(self, name):
        zone = self.service.get_zone_by_name(name)
        return Zone(self, zone)

    def create_host_zone(self, name, jump_start=False):
        host_service = self.get_host_service()
        host_service.full_zone_set(name, self.user_key, jump_start)
        zone = self.get_zone_by_name(name)

        # Zone created by using Host API contains some garbage records.
        # We should remove them before creating our owns.
        for record in zone.iter_records():
            record.delete()

        return zone

    def create_zone(self, name, jump_start=False, organization=None):
        zone = self.service.create_zone(name=name, jump_start=jump_start,
                                        organization=organization)
        return Zone(self, zone)

    def __repr__(self):
        return 'User<%s>' % self.email


class Zone(object):
    def __init__(self, user, data):
        self.user = user
        self.service = user.service
        self._data = data

    def __getattr__(self, name):
        if name in self._data:
            return self._data[name]
        raise AttributeError()

    def delete(self):
        self.service.delete_zone(self.id)
        clear_property_cache(self.user, 'zones')

    @cached_property
    def settings(self):
        return ZoneSettings(self)

    def iter_records(self):
        for record in CloudFlarePageIterator(
                self.service.get_dns_records, args=(self.id,)):
            yield Record(self, record)

    @cached_property
    def records(self):
        by_name = {}
        for record in self.iter_records():
            by_name.setdefault(record.name, []).append(record)
        for value in itervalues(by_name):
            value.sort(key=lambda r: (r.type, r.content))
        return by_name

    def create_record(self, name, type, content, ttl=1, proxied=False,
                      priority=10):
        data = {
            'name': name,
            'type': type,
            'content': content,
            'ttl': ttl,
            'proxied': proxied,
        }
        if type == 'MX':
            data['priority'] = priority

        record = self.service.create_dns_record(self.id, data)
        clear_property_cache(self, 'records')
        return Record(self, record)

    def __repr__(self):
        return 'Zone<%s>' % self.name


class ZoneSettings(object):
    def __init__(self, zone):
        self.zone = zone
        self.service = zone.service
        self._get_settings()
        self._updates = {}

    def _get_settings(self):
        self._settings = {}
        for setting in CloudFlarePageIterator(
                self.service.get_zone_settings, args=(self.zone.id,)):
            self._settings[setting['id']] = setting

    def __getattr__(self, name):
        if name in self._updates:
            return self._updates[name]
        if name in self._settings:
            return self._settings[name]['value']
        raise AttributeError()

    def __setattr__(self, name, value):
        if name in ('zone', 'service', '_settings', '_updates'):
            return super(ZoneSettings, self).__setattr__(name, value)
        if name not in self._settings:
            raise AttributeError('Not a valid setting')
        if not self._settings[name]['editable']:
            raise ValueError('Not an editeable setting')
        self._updates[name] = value

    def save(self):
        if not self._updates:
            return
        items = [{'id': name, 'value': value}
                 for name, value in iteritems(self._updates)]
        self.service.set_zone_settings(self.zone.id, items)
        self._get_settings()
        self._updates = {}

    def __iter__(self):
        return iter(sorted(self._settings))

    def __repr__(self):
        return 'ZoneSettings<%s>' % self.zone.name


class Record(object):
    _data = ()

    def __init__(self, zone, data):
        self.zone = zone
        self.service = zone.service
        self._data = data
        self._updates = {}

    def __getattr__(self, name):
        if name in self._updates:
            return self._updates[name]
        if name in self._data:
            return self._data[name]
        raise AttributeError()

    def __setattr__(self, name, value):
        if name in ('zone', 'service', '_data', '_updates'):
            return super(Record, self).__setattr__(name, value)
        if name in self._data:
            self._updates[name] = value
        else:
            raise AttributeError()

    def save(self):
        if self._updates:
            result = self.service.update_dns_record(self.zone.id, self.id,
                                                    self._updates)
            self._data.update(result)
            if 'name' in self._updates:
                clear_property_cache(self.zone, 'records')
            self._updates = {}

    def delete(self):
        self.service.delete_dns_record(self.zone.id, self.id)
        clear_property_cache(self.zone, 'records')

    def __repr__(self):
        return 'Record<%s %s IN %s %s>' % (self.name, self.ttl, self.type,
                                           self.content)
