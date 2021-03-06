import json
import urllib2
import re
import requests

from bsdapi.BsdApi import Factory as BSDApiFactory
from django.conf import settings
from make_mozilla.events.models import Event, Venue, EventAndVenueUpdater
from make_mozilla.bsd.extractors import json as json_extractors
from make_mozilla.bsd.extractors import xml as xml_extractors
import email.parser
import commonware.log
import funfactory.log_settings # Magic voodoo required to make logging work.

log = commonware.log.getLogger('mk.bsd')

def parse_event_feed(feed_url):
    return process_events_json(json.load(urllib2.urlopen(feed_url)))

def process_events_json(events_json):
    return [event['url'] for event in events_json['results']]

def fetch_and_process_event_feed(event_kind, feed_url):
    [BSDEventImporter.process_event(event_kind, url) for url in parse_event_feed(feed_url)]

class BSDClient(object):
    @classmethod
    def _request(cls, http_method, endpoint, api_params):
        client = cls.create_api_client()
        response = client.doRequest(endpoint,
                api_params = api_params,
                request_type = http_method,
                https = True)
        return response

    @classmethod
    def _get(cls, endpoint, api_params):
        return cls._request('GET', endpoint, api_params)

    @classmethod
    def _post(cls, endpoint, api_params):
        return cls._request('POST', endpoint, api_params)

    @classmethod
    def fetch_event(cls, obfuscated_event_id):
        return json.loads(cls.fetch_event_body(obfuscated_event_id))

    @classmethod
    def fetch_event_body(cls, obfuscated_event_id):
        response = cls._get('/event/get_event_details',
                {'values': json.dumps({'event_id_obfuscated': obfuscated_event_id})})
        return response.body

    @classmethod
    def _api_response_charset(cls, api_response):
        content_type_header = api_response.http_response.getheader('content-type',
                'text/xml; charset=utf-8')
        m = email.parser.Parser().parsestr("Content-Type: %s" % content_type_header)
        charset = (m.get_param('charset') or 'utf-8')
        return charset

    @classmethod
    def constituent_email_for_constituent_id(cls, constituent_id):
        response = cls._get('/cons/get_constituents_by_id',
                {'cons_ids': constituent_id, 'bundles': 'primary_cons_email'})
        charset = cls._api_response_charset(response)
        return xml_extractors.constituent_email(response.body.encode(charset))

    @classmethod
    def create_api_client(cls):
        client_params = {'port': 80, 'securePort': 443}
        client_params.update(settings.BSD_API_DETAILS)
        return BSDApiFactory().create(**client_params)

    @classmethod
    def register_email_address_as_constituent(cls, email_address):
        response = cls._post('/cons/email_register',
                {'email': email_address, 'format': 'json'})
        if response.http_status == 200:
            api_response = json.loads(response.body)
            return api_response[0]['cons_id']
        raise BSDApiError("%s: %s" % (response.http_status, response.http_reason))

    @classmethod
    def add_constituent_id_to_group(cls, cons_id, group_id):
        response = cls._post('/cons_group/add_cons_ids_to_group',
                {'cons_ids': cons_id, 'cons_group_id': group_id})
        return response.http_status == 202

class BSDRegisterConstituent(object):
    @classmethod
    def add_email_to_group(cls, email, group_id):
        constituent_id = BSDClient.register_email_address_as_constituent(email)
        result = BSDClient.add_constituent_id_to_group(constituent_id, group_id)
        if not result:
            log.warning('Failed to add email to group')
        return result

class BSDEventImporter(object):
    @classmethod
    def extract_event_obfuscated_id(cls, event_url):
        return re.split(r'/', event_url)[-1]

    @classmethod
    def process_event(cls, event_kind, event_url):
        obfuscated_id = cls.extract_event_obfuscated_id(event_url)
        event_json = BSDClient.fetch_event(obfuscated_id)
        # BSDEventImporter() rather than cls() because of test mocking
        BSDEventImporter().process_event_from_json(event_kind, event_url, event_json)

    def event_extractors(self):
        return [json_extractors.event_name, json_extractors.event_times, json_extractors.event_description, json_extractors.event_official]

    def venue_extractors(self):
        return [json_extractors.venue_name, json_extractors.venue_country, 
                json_extractors.venue_street_address, json_extractors.venue_location]

    def fetch_existing_event(self, source_id):
        try:
            return Event.objects.get(source = 'bsd', source_id = source_id)
        except Event.DoesNotExist:
            return None

    def fetch_organiser_email_from_bsd(self, event_json):
        constituent_id = event_json['creator_cons_id']

        return BSDClient.constituent_email_for_constituent_id(constituent_id)

    def venue_for_event(self, event):
        if event.id is not None:
            return event.venue
        return Venue()

    def extract_from_event_json(self, event_json):
        event_attrs = {}
        [event_attrs.update(f(event_json)) for f in self.event_extractors()]
        venue_attrs = {}
        [venue_attrs.update(f(event_json)) for f in self.venue_extractors()]
        return {'event': event_attrs, 'venue': venue_attrs}

    def new_models_from_json(self, event_json):
        model_data = self.extract_from_event_json(event_json)
        event = Event(**model_data['event'])
        venue = Venue(**model_data['venue'])
        return (event, venue)

    def process_event_from_json(self, event_kind, event_url, event_json):
        source_id = event_json['event_id']
        event = self.fetch_existing_event(source_id)
        if event is None:
            event = Event()
        organiser_email = self.fetch_organiser_email_from_bsd(event_json)
        venue = self.venue_for_event(event)
        (new_event, new_venue) = self.new_models_from_json(event_json)
        new_event.event_url = event_url
        new_event.source_id = source_id
        new_event.source = 'bsd'
        new_event.organiser_email = organiser_email
        new_event.kind = event_kind
        new_event.public = True
        new_event.verified = True
        if event.id:
            log.info('Updating event %s from %s' % (event.id, event_url))
        else:
            log.info('Adding new event for %s' % event_url)
        EventAndVenueUpdater.update(event, new_event, venue, new_venue)

class BSDReaper(object):
    def __init__(self, chunks, chunk_to_process):
        self.chunks = chunks
        self.chunk_to_process = chunk_to_process

    def subset(self, input_set):
        for event in input_set:
            if event.pk % self.chunks == self.chunk_to_process:
                yield event

    def process(self):
        input_query = Event.all_upcoming_bsd()
        if input_query.count() > 50:
            input_query = self.subset(input_query)
        for event in input_query:
            response = requests.head(event.event_url)
            if response.status_code == 404:
                log.info('Deleting event %s' % event.event_url)
                event.delete()

class BSDApiError(BaseException):
    pass
