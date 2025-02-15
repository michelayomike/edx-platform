"""Tests for items views."""

from __future__ import absolute_import

import copy
import json
import tempfile
import textwrap
from codecs import BOM_UTF8
from uuid import uuid4

import ddt
import six
from django.conf import settings
from django.test.utils import override_settings
from django.urls import reverse
from edxval.api import create_video
from mock import Mock, patch
from opaque_keys.edx.keys import UsageKey

from contentstore.tests.utils import CourseTestCase, mock_requests_get
from openedx.core.djangoapps.contentserver.caching import del_cached_content
from xmodule.contentstore.content import StaticContent
from xmodule.contentstore.django import contentstore
from xmodule.exceptions import NotFoundError
from xmodule.modulestore.django import modulestore
from xmodule.video_module import VideoBlock
from xmodule.video_module.transcripts_utils import (
    GetTranscriptsFromYouTubeException,
    Transcript,
    get_video_transcript_content,
    remove_subs_from_store
)

TEST_DATA_CONTENTSTORE = copy.deepcopy(settings.CONTENTSTORE)
TEST_DATA_CONTENTSTORE['DOC_STORE_CONFIG']['db'] = 'test_xcontent_%s' % uuid4().hex

SRT_TRANSCRIPT_CONTENT = u"""0
00:00:10,500 --> 00:00:13,000
Elephant's Dream

1
00:00:15,000 --> 00:00:18,000
At the left we can see...

"""

SJSON_TRANSCRIPT_CONTENT = Transcript.convert(
    SRT_TRANSCRIPT_CONTENT,
    Transcript.SRT,
    Transcript.SJSON,
)


@override_settings(CONTENTSTORE=TEST_DATA_CONTENTSTORE)
class BaseTranscripts(CourseTestCase):
    """Base test class for transcripts tests."""

    def clear_subs_content(self):
        """Remove, if transcripts content exists."""
        for youtube_id in self.get_youtube_ids().values():
            filename = 'subs_{0}.srt.sjson'.format(youtube_id)
            content_location = StaticContent.compute_location(self.course.id, filename)
            try:
                content = contentstore().find(content_location)
                contentstore().delete(content.get_id())
            except NotFoundError:
                pass

    def save_subs_to_store(self, subs, subs_id):
        """
        Save transcripts into `StaticContent`.
        """
        filedata = json.dumps(subs, indent=2)
        mime_type = 'application/json'
        filename = 'subs_{0}.srt.sjson'.format(subs_id)

        content_location = StaticContent.compute_location(self.course.id, filename)
        content = StaticContent(content_location, filename, mime_type, filedata)
        contentstore().save(content)
        del_cached_content(content_location)
        return content_location

    def setUp(self):
        """Create initial data."""
        super(BaseTranscripts, self).setUp()

        # Add video module
        data = {
            'parent_locator': six.text_type(self.course.location),
            'category': 'video',
            'type': 'video'
        }
        resp = self.client.ajax_post('/xblock/', data)
        self.assertEqual(resp.status_code, 200)

        self.video_usage_key = self._get_usage_key(resp)
        self.item = modulestore().get_item(self.video_usage_key)
        # hI10vDNYz4M - valid Youtube ID with transcripts.
        # JMD_ifUUfsU, AKqURZnYqpk, DYpADpL7jAY - valid Youtube IDs without transcripts.
        self.set_fields_from_xml(
            self.item, '<video youtube="0.75:JMD_ifUUfsU,1.0:hI10vDNYz4M,1.25:AKqURZnYqpk,1.50:DYpADpL7jAY" />'
        )
        modulestore().update_item(self.item, self.user.id)

        self.item = modulestore().get_item(self.video_usage_key)
        # Remove all transcripts for current module.
        self.clear_subs_content()

    def _get_usage_key(self, resp):
        """ Returns the usage key from the response returned by a create operation. """
        usage_key_string = json.loads(resp.content.decode('utf-8')).get('locator')
        return UsageKey.from_string(usage_key_string)

    def get_youtube_ids(self):
        """Return youtube speeds and ids."""
        item = modulestore().get_item(self.video_usage_key)

        return {
            0.75: item.youtube_id_0_75,
            1: item.youtube_id_1_0,
            1.25: item.youtube_id_1_25,
            1.5: item.youtube_id_1_5
        }

    def create_non_video_module(self):
        """
        Setup non video module for tests.
        """
        data = {
            'parent_locator': six.text_type(self.course.location),
            'category': 'non_video',
            'type': 'non_video'
        }
        response = self.client.ajax_post('/xblock/', data)
        usage_key = self._get_usage_key(response)
        item = modulestore().get_item(usage_key)
        self.set_fields_from_xml(self.item, '<non_video youtube="0.75:JMD_ifUUfsU,1.0:hI10vDNYz4M" />')
        modulestore().update_item(item, self.user.id)

        return usage_key

    def assert_response(self, response, expected_status_code, expected_message):
        response_content = json.loads(response.content.decode('utf-8'))
        self.assertEqual(response.status_code, expected_status_code)
        self.assertEqual(response_content['status'], expected_message)

    def set_fields_from_xml(self, item, xml):
        fields_data = VideoBlock.parse_video_xml(xml)
        for key, value in fields_data.items():
            setattr(item, key, value)


@ddt.ddt
class TestUploadTranscripts(BaseTranscripts):
    """
    Tests for '/transcripts/upload' endpoint.
    """
    def setUp(self):
        super(TestUploadTranscripts, self).setUp()
        self.contents = {
            'good': SRT_TRANSCRIPT_CONTENT,
            'bad': u'Some BAD data',
        }
        # Create temporary transcript files
        self.good_srt_file = self.create_transcript_file(content=self.contents['good'], suffix='.srt')
        self.bad_data_srt_file = self.create_transcript_file(content=self.contents['bad'], suffix='.srt')
        self.bad_name_srt_file = self.create_transcript_file(content=self.contents['good'], suffix='.bad')
        self.bom_srt_file = self.create_transcript_file(content=self.contents['good'], suffix='.srt', include_bom=True)

        # Setup a VEDA produced video and persist `edx_video_id` in VAL.
        create_video({
            'edx_video_id': u'123-456-789',
            'status': 'upload',
            'client_video_id': u'Test Video',
            'duration': 0,
            'encoded_videos': [],
            'courses': [six.text_type(self.course.id)]
        })

        # Add clean up handler
        self.addCleanup(self.clean_temporary_transcripts)

    def create_transcript_file(self, content, suffix, include_bom=False):
        """
        Setup a transcript file with suffix and content.
        """
        transcript_file = tempfile.NamedTemporaryFile(suffix=suffix)
        wrapped_content = textwrap.dedent(content)
        if include_bom:
            wrapped_content = wrapped_content.encode('utf-8-sig')
            # Verify that ufeff(BOM) character is in content.
            self.assertIn(BOM_UTF8, wrapped_content)
            transcript_file.write(wrapped_content)
        else:
            transcript_file.write(wrapped_content.encode('utf-8'))
        transcript_file.seek(0)

        return transcript_file

    def clean_temporary_transcripts(self):
        """
        Close transcript files gracefully.
        """
        self.good_srt_file.close()
        self.bad_data_srt_file.close()
        self.bad_name_srt_file.close()
        self.bom_srt_file.close()

    def upload_transcript(self, locator, transcript_file, edx_video_id=None):
        """
        Uploads a transcript for a video
        """
        payload = {}
        if locator:
            payload.update({'locator': locator})

        if edx_video_id is not None:
            payload.update({'edx_video_id': edx_video_id})

        if transcript_file:
            payload.update({'transcript-file': transcript_file})

        upload_url = reverse('upload_transcripts')
        response = self.client.post(upload_url, payload)

        return response

    @ddt.data(
        (u'123-456-789', False),
        (u'', False),
        (u'123-456-789', True)
    )
    @ddt.unpack
    def test_transcript_upload_success(self, edx_video_id, include_bom):
        """
        Tests transcript file upload to video component works as
        expected in case of following:

         1. External video component
         2. VEDA produced video component
         3. Transcript content containing BOM character
        """
        # In case of an external video component, the `edx_video_id` must be empty
        # and VEDA produced video component will have `edx_video_id` set to VAL video ID.
        self.item.edx_video_id = edx_video_id
        modulestore().update_item(self.item, self.user.id)

        # Upload a transcript
        transcript_file = self.bom_srt_file if include_bom else self.good_srt_file
        response = self.upload_transcript(self.video_usage_key, transcript_file, '')

        # Verify the response
        self.assert_response(response, expected_status_code=200, expected_message='Success')

        # Verify the `edx_video_id` on the video component
        json_response = json.loads(response.content.decode('utf-8'))
        expected_edx_video_id = edx_video_id if edx_video_id else json_response['edx_video_id']
        video = modulestore().get_item(self.video_usage_key)
        self.assertEqual(video.edx_video_id, expected_edx_video_id)

        # Verify transcript content
        actual_transcript = get_video_transcript_content(video.edx_video_id, language_code=u'en')
        actual_sjson_content = json.loads(actual_transcript['content'])
        expected_sjson_content = json.loads(Transcript.convert(
            self.contents['good'],
            input_format=Transcript.SRT,
            output_format=Transcript.SJSON
        ))
        self.assertDictEqual(actual_sjson_content, expected_sjson_content)

    def test_transcript_upload_without_locator(self):
        """
        Test that transcript upload validation fails if the video locator is missing
        """
        response = self.upload_transcript(locator=None, transcript_file=self.good_srt_file, edx_video_id='')
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Video locator is required.'
        )

    def test_transcript_upload_without_file(self):
        """
        Test that transcript upload validation fails if transcript file is missing
        """
        response = self.upload_transcript(locator=self.video_usage_key, transcript_file=None, edx_video_id='')
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'A transcript file is required.'
        )

    def test_transcript_upload_bad_format(self):
        """
        Test that transcript upload validation fails if transcript format is not SRT
        """
        response = self.upload_transcript(
            locator=self.video_usage_key,
            transcript_file=self.bad_name_srt_file,
            edx_video_id=''
        )
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'This transcript file type is not supported.'
        )

    def test_transcript_upload_bad_content(self):
        """
        Test that transcript upload validation fails in case of bad transcript content.
        """
        # Request to upload transcript for the video
        response = self.upload_transcript(
            locator=self.video_usage_key,
            transcript_file=self.bad_data_srt_file,
            edx_video_id=''
        )
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'There is a problem with this transcript file. Try to upload a different file.'
        )

    def test_transcript_upload_unknown_category(self):
        """
        Test that transcript upload validation fails if item's category is other than video.
        """
        # non_video module setup - i.e. an item whose category is not 'video'.
        usage_key = self.create_non_video_module()
        # Request to upload transcript for the item
        response = self.upload_transcript(locator=usage_key, transcript_file=self.good_srt_file, edx_video_id='')
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Transcripts are supported only for "video" modules.'
        )

    def test_transcript_upload_non_existent_item(self):
        """
        Test that transcript upload validation fails in case of invalid item's locator.
        """
        # Request to upload transcript for the item
        response = self.upload_transcript(
            locator='non_existent_locator',
            transcript_file=self.good_srt_file,
            edx_video_id=''
        )
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Cannot find item by locator.'
        )

    def test_transcript_upload_without_edx_video_id(self):
        """
        Test that transcript upload validation fails if the `edx_video_id` is missing
        """
        response = self.upload_transcript(locator=self.video_usage_key, transcript_file=self.good_srt_file)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Video ID is required.'
        )

    def test_transcript_upload_with_non_existant_edx_video_id(self):
        """
        Test that transcript upload works as expected if `edx_video_id` set on
        video descriptor is different from `edx_video_id` received in POST request.
        """
        non_existant_edx_video_id = '1111-2222-3333-4444'

        # Upload with non-existant `edx_video_id`
        response = self.upload_transcript(
            locator=self.video_usage_key,
            transcript_file=self.good_srt_file,
            edx_video_id=non_existant_edx_video_id
        )
        # Verify the response
        self.assert_response(response, expected_status_code=400, expected_message='Invalid Video ID')

        # Verify transcript does not exist for non-existant `edx_video_id`
        self.assertIsNone(get_video_transcript_content(non_existant_edx_video_id, language_code=u'en'))


@ddt.ddt
class TestChooseTranscripts(BaseTranscripts):
    """
    Tests for '/transcripts/choose' endpoint.
    """
    def setUp(self):
        super(TestChooseTranscripts, self).setUp()

        # Create test transcript in contentstore
        self.chosen_html5_id = 'test_html5_subs'
        self.sjson_subs = Transcript.convert(SRT_TRANSCRIPT_CONTENT, Transcript.SRT, Transcript.SJSON)
        self.save_subs_to_store(json.loads(self.sjson_subs), self.chosen_html5_id)

        # Setup a VEDA produced video and persist `edx_video_id` in VAL.
        create_video({
            'edx_video_id': u'123-456-789',
            'status': 'upload',
            'client_video_id': u'Test Video',
            'duration': 0,
            'encoded_videos': [],
            'courses': [six.text_type(self.course.id)]
        })

    def choose_transcript(self, locator, chosen_html5_id):
        """
        Make an endpoint call to choose transcript
        """
        payload = {}
        if locator:
            payload.update({'locator': six.text_type(locator)})

        if chosen_html5_id:
            payload.update({'html5_id': chosen_html5_id})

        choose_transcript_url = reverse('choose_transcripts')
        response = self.client.get(choose_transcript_url, {'data': json.dumps(payload)})
        return response

    @ddt.data(u'123-456-789', u'')
    def test_choose_transcript_success(self, edx_video_id):
        """
        Verify that choosing transcript file in video component basic tab works as
        expected in case of following:

         1. External video component
         2. VEDA produced video component
        """
        # In case of an external video component, the `edx_video_id` must be empty
        # and VEDA produced video component will have `edx_video_id` set to VAL video ID.
        self.item.edx_video_id = edx_video_id
        modulestore().update_item(self.item, self.user.id)

        # Make call to choose a transcript
        response = self.choose_transcript(self.video_usage_key, self.chosen_html5_id)

        # Verify the response
        self.assert_response(response, expected_status_code=200, expected_message='Success')

        # Verify the `edx_video_id` on the video component
        json_response = json.loads(response.content.decode('utf-8'))
        expected_edx_video_id = edx_video_id if edx_video_id else json_response['edx_video_id']
        video = modulestore().get_item(self.video_usage_key)
        self.assertEqual(video.edx_video_id, expected_edx_video_id)

        # Verify transcript content
        actual_transcript = get_video_transcript_content(video.edx_video_id, language_code=u'en')
        actual_sjson_content = json.loads(actual_transcript['content'])
        expected_sjson_content = json.loads(self.sjson_subs)
        self.assertDictEqual(actual_sjson_content, expected_sjson_content)

    def test_choose_transcript_fails_without_data(self):
        """
        Verify that choose transcript fails if we do not provide video data in request.
        """
        response = self.choose_transcript(locator=None, chosen_html5_id=None)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Incoming video data is empty.'
        )

    def test_choose_transcript_fails_without_locator(self):
        """
        Verify that choose transcript fails if video locator is missing in request.
        """
        response = self.choose_transcript(locator=None, chosen_html5_id=self.chosen_html5_id)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Cannot find item by locator.'
        )

    def test_choose_transcript_with_no_html5_transcript(self):
        """
        Verify that choose transcript fails if the chosen html5 ID don't
        have any transcript associated in contentstore.
        """
        response = self.choose_transcript(locator=self.video_usage_key, chosen_html5_id='non-existent-html5-id')
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u"No such transcript."
        )

    def test_choose_transcript_fails_on_unknown_category(self):
        """
        Test that transcript choose validation fails if item's category is other than video.
        """
        # non_video module setup - i.e. an item whose category is not 'video'.
        usage_key = self.create_non_video_module()
        # Request to choose transcript for the item
        response = self.choose_transcript(locator=usage_key, chosen_html5_id=self.chosen_html5_id)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Transcripts are supported only for "video" modules.'
        )


@ddt.ddt
class TestRenameTranscripts(BaseTranscripts):
    """
    Tests for '/transcripts/rename' endpoint.
    """
    def setUp(self):
        super(TestRenameTranscripts, self).setUp()

        # Create test transcript in contentstore and update item's sub.
        self.item.sub = 'test_video_subs'
        self.sjson_subs = Transcript.convert(SRT_TRANSCRIPT_CONTENT, Transcript.SRT, Transcript.SJSON)
        self.save_subs_to_store(json.loads(self.sjson_subs), self.item.sub)
        modulestore().update_item(self.item, self.user.id)

        # Setup a VEDA produced video and persist `edx_video_id` in VAL.
        create_video({
            'edx_video_id': u'123-456-789',
            'status': 'upload',
            'client_video_id': u'Test Video',
            'duration': 0,
            'encoded_videos': [],
            'courses': [six.text_type(self.course.id)]
        })

    def rename_transcript(self, locator):
        """
        Make an endpoint call to rename transcripts.
        """
        payload = {}
        if locator:
            payload.update({'locator': six.text_type(locator)})

        rename_transcript_url = reverse('rename_transcripts')
        response = self.client.get(rename_transcript_url, {'data': json.dumps(payload)})
        return response

    @ddt.data(u'123-456-789', u'')
    def test_rename_transcript_success(self, edx_video_id):
        """
        Verify that "use current transcript" in video component basic tab works as
        expected in case of following:

         1. External video component
         2. VEDA produced video component
        """
        # In case of an external video component, the `edx_video_id` must be empty
        # and VEDA produced video component will have `edx_video_id` set to VAL video ID.
        self.item.edx_video_id = edx_video_id
        modulestore().update_item(self.item, self.user.id)

        # Make call to use current transcript from contentstore
        response = self.rename_transcript(self.video_usage_key)

        # Verify the response
        self.assert_response(response, expected_status_code=200, expected_message='Success')

        # Verify the `edx_video_id` on the video component
        json_response = json.loads(response.content.decode('utf-8'))
        expected_edx_video_id = edx_video_id if edx_video_id else json_response['edx_video_id']
        video = modulestore().get_item(self.video_usage_key)
        self.assertEqual(video.edx_video_id, expected_edx_video_id)

        # Verify transcript content
        actual_transcript = get_video_transcript_content(video.edx_video_id, language_code=u'en')
        actual_sjson_content = json.loads(actual_transcript['content'])
        expected_sjson_content = json.loads(self.sjson_subs)
        self.assertDictEqual(actual_sjson_content, expected_sjson_content)

    def test_rename_transcript_fails_without_data(self):
        """
        Verify that use current transcript fails if we do not provide video data in request.
        """
        response = self.rename_transcript(locator=None)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Incoming video data is empty.'
        )

    def test_rename_transcript_fails_with_invalid_locator(self):
        """
        Verify that use current transcript fails if video locator is missing in request.
        """
        response = self.rename_transcript(locator='non-existent-locator')
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Cannot find item by locator.'
        )

    def test_rename_transcript_with_non_existent_sub(self):
        """
        Verify that rename transcript fails if the `item.sub` don't
        have any transcript associated in contentstore.
        """
        # Update item's sub to an id who does not have any
        # transcript associated in contentstore.
        self.item.sub = 'non-existent-sub'
        modulestore().update_item(self.item, self.user.id)

        response = self.rename_transcript(locator=self.video_usage_key)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u"No such transcript."
        )

    def test_rename_transcript_fails_on_unknown_category(self):
        """
        Test that validation fails if item's category is other than video.
        """
        # non_video module setup - i.e. an item whose category is not 'video'.
        usage_key = self.create_non_video_module()
        # Make call to use current transcript from contentstore.
        response = self.rename_transcript(usage_key)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Transcripts are supported only for "video" modules.'
        )


@ddt.ddt
@patch('contentstore.views.transcripts_ajax.download_youtube_subs', Mock(return_value=SJSON_TRANSCRIPT_CONTENT))
class TestReplaceTranscripts(BaseTranscripts):
    """
    Tests for '/transcripts/replace' endpoint.
    """
    def setUp(self):
        super(TestReplaceTranscripts, self).setUp()
        self.youtube_id = 'test_yt_id'

        # Setup a VEDA produced video and persist `edx_video_id` in VAL.
        create_video({
            'edx_video_id': u'123-456-789',
            'status': 'upload',
            'client_video_id': u'Test Video',
            'duration': 0,
            'encoded_videos': [],
            'courses': [six.text_type(self.course.id)]
        })

    def replace_transcript(self, locator, youtube_id):
        """
        Make an endpoint call to replace transcripts with youtube ones.
        """
        payload = {}
        if locator:
            payload.update({'locator': six.text_type(locator)})

        if youtube_id:
            payload.update({
                'videos': [
                    {
                        'type': 'youtube',
                        'video': youtube_id
                    }
                ]
            })

        replace_transcript_url = reverse('replace_transcripts')
        response = self.client.get(replace_transcript_url, {'data': json.dumps(payload)})
        return response

    @ddt.data(u'123-456-789', u'')
    def test_replace_transcript_success(self, edx_video_id):
        """
        Verify that "import from youtube" in video component basic tab works as
        expected in case of following:

         1. External video component
         2. VEDA produced video component
        """
        # In case of an external video component, the `edx_video_id` must be empty
        # and VEDA produced video component will have `edx_video_id` set to VAL video ID.
        self.item.edx_video_id = edx_video_id
        modulestore().update_item(self.item, self.user.id)

        # Make call to replace transcripts from youtube
        response = self.replace_transcript(self.video_usage_key, self.youtube_id)

        # Verify the response
        self.assert_response(response, expected_status_code=200, expected_message='Success')

        # Verify the `edx_video_id` on the video component
        json_response = json.loads(response.content.decode('utf-8'))
        expected_edx_video_id = edx_video_id if edx_video_id else json_response['edx_video_id']
        video = modulestore().get_item(self.video_usage_key)
        self.assertEqual(video.edx_video_id, expected_edx_video_id)

        # Verify transcript content
        actual_transcript = get_video_transcript_content(video.edx_video_id, language_code=u'en')
        actual_sjson_content = json.loads(actual_transcript['content'])
        expected_sjson_content = json.loads(SJSON_TRANSCRIPT_CONTENT)
        self.assertDictEqual(actual_sjson_content, expected_sjson_content)

    def test_replace_transcript_fails_without_data(self):
        """
        Verify that replace transcript fails if we do not provide video data in request.
        """
        response = self.replace_transcript(locator=None, youtube_id=None)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Incoming video data is empty.'
        )

    def test_replace_transcript_fails_with_invalid_locator(self):
        """
        Verify that replace transcript fails if a video locator does not exist.
        """
        response = self.replace_transcript(locator='non-existent-locator', youtube_id=self.youtube_id)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Cannot find item by locator.'
        )

    def test_replace_transcript_fails_without_yt_id(self):
        """
        Verify that replace transcript fails if youtube id is not provided.
        """
        response = self.replace_transcript(locator=self.video_usage_key, youtube_id=None)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'YouTube ID is required.'
        )

    def test_replace_transcript_no_transcript_on_yt(self):
        """
        Verify that replace transcript fails if YouTube does not have transcript for the given youtube id.
        """
        error_message = u'YT ID not found.'
        with patch('contentstore.views.transcripts_ajax.download_youtube_subs') as mock_download_youtube_subs:
            mock_download_youtube_subs.side_effect = GetTranscriptsFromYouTubeException(error_message)
            response = self.replace_transcript(locator=self.video_usage_key, youtube_id='non-existent-yt-id')
            self.assertContains(response, text=error_message, status_code=400)

    def test_replace_transcript_fails_on_unknown_category(self):
        """
        Test that validation fails if item's category is other than video.
        """
        # non_video module setup - i.e. an item whose category is not 'video'.
        usage_key = self.create_non_video_module()
        response = self.replace_transcript(usage_key, youtube_id=self.youtube_id)
        self.assert_response(
            response,
            expected_status_code=400,
            expected_message=u'Transcripts are supported only for "video" modules.'
        )


class TestDownloadTranscripts(BaseTranscripts):
    """
    Tests for '/transcripts/download' url.
    """
    def update_video_component(self, sub=None, youtube_id=None):
        """
        Updates video component with `sub` and `youtube_id`.
        """
        sjson_transcript = json.loads(SJSON_TRANSCRIPT_CONTENT)
        self.item.sub = sub
        if sub:
            self.save_subs_to_store(sjson_transcript, sub)
        self.item.youtube_id_1_0 = youtube_id
        if youtube_id:
            self.save_subs_to_store(sjson_transcript, youtube_id)

        modulestore().update_item(self.item, self.user.id)

    def download_transcript(self, locator):
        """
        Makes a call to download transcripts.
        """
        payload = {}
        if locator:
            payload.update({'locator': six.text_type(locator)})

        download_transcript_url = reverse('download_transcripts')
        response = self.client.get(download_transcript_url, payload)
        return response

    def assert_download_response(self, response, expected_status_code, expected_content=None):
        """
        Verify transcript download response.
        """
        self.assertEqual(response.status_code, expected_status_code)
        if expected_content:
            assert response.content.decode('utf-8') == expected_content

    def test_download_youtube_transcript_success(self):
        """
        Verify that the transcript associated to YT id is downloaded successfully.
        """
        self.update_video_component(youtube_id='JMD_ifUUfsU')
        response = self.download_transcript(locator=self.video_usage_key)
        self.assert_download_response(response, expected_content=SRT_TRANSCRIPT_CONTENT, expected_status_code=200)

    def test_download_non_youtube_transcript_success(self):
        """
        Verify that the transcript associated to item's `sub` is downloaded successfully.
        """
        self.update_video_component(sub='test_subs')
        response = self.download_transcript(locator=self.video_usage_key)
        self.assert_download_response(response, expected_content=SRT_TRANSCRIPT_CONTENT, expected_status_code=200)

    def test_download_transcript_404_without_locator(self):
        """
        Verify that download transcript returns 404 without locator.
        """
        response = self.download_transcript(locator=None)
        self.assert_download_response(response, expected_status_code=404)

    def test_download_transcript_404_with_bad_locator(self):
        """
        Verify that download transcript returns 404 with invalid locator.
        """
        response = self.download_transcript(locator='invalid-locator')
        self.assert_download_response(response, expected_status_code=404)

    def test_download_transcript_404_for_non_video_module(self):
        """
        Verify that download transcript returns 404 for a non video module.
        """
        usage_key = self.create_non_video_module()
        response = self.download_transcript(locator=usage_key)
        self.assert_download_response(response, expected_status_code=404)

    def test_download_transcript_404_for_no_yt_and_no_sub(self):
        """
        Verify that download transcript returns 404 when video component
        does not have sub and youtube id.
        """
        self.update_video_component(sub=None, youtube_id=None)
        response = self.download_transcript(locator=self.video_usage_key)
        self.assert_download_response(response, expected_status_code=404)


@ddt.ddt
class TestCheckTranscripts(BaseTranscripts):
    """
    Tests for '/transcripts/check' url.
    """
    def test_success_download_nonyoutube(self):
        subs_id = str(uuid4())
        self.set_fields_from_xml(self.item, u"""
            <video youtube="" sub="{}">
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.mp4"/>
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.webm"/>
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.ogv"/>
            </video>
        """.format(subs_id))
        modulestore().update_item(self.item, self.user.id)

        subs = {
            'start': [100, 200, 240],
            'end': [200, 240, 380],
            'text': [
                'subs #1',
                'subs #2',
                'subs #3'
            ]
        }
        self.save_subs_to_store(subs, subs_id)

        data = {
            'locator': six.text_type(self.video_usage_key),
            'videos': [{
                'type': 'html5',
                'video': subs_id,
                'mode': 'mp4',
            }]
        }
        link = reverse('check_transcripts')
        resp = self.client.get(link, {'data': json.dumps(data)})
        self.assertEqual(resp.status_code, 200)
        self.assertDictEqual(
            json.loads(resp.content.decode('utf-8')),
            {
                u'status': u'Success',
                u'youtube_local': False,
                u'is_youtube_mode': False,
                u'youtube_server': False,
                u'command': u'found',
                u'current_item_subs': six.text_type(subs_id),
                u'youtube_diff': True,
                u'html5_local': [six.text_type(subs_id)],
                u'html5_equal': False,
            }
        )

        remove_subs_from_store(subs_id, self.item)

    def test_check_youtube(self):
        self.set_fields_from_xml(self.item, '<video youtube="1:JMD_ifUUfsU" />')
        modulestore().update_item(self.item, self.user.id)

        subs = {
            'start': [100, 200, 240],
            'end': [200, 240, 380],
            'text': [
                'subs #1',
                'subs #2',
                'subs #3'
            ]
        }
        self.save_subs_to_store(subs, 'JMD_ifUUfsU')
        link = reverse('check_transcripts')
        data = {
            'locator': six.text_type(self.video_usage_key),
            'videos': [{
                'type': 'youtube',
                'video': 'JMD_ifUUfsU',
                'mode': 'youtube',
            }]
        }

        resp = self.client.get(link, {'data': json.dumps(data)})

        self.assertEqual(resp.status_code, 200)
        self.assertDictEqual(
            json.loads(resp.content.decode('utf-8')),
            {
                u'status': u'Success',
                u'youtube_local': True,
                u'is_youtube_mode': True,
                u'youtube_server': False,
                u'command': u'found',
                u'current_item_subs': None,
                u'youtube_diff': True,
                u'html5_local': [],
                u'html5_equal': False,
            }
        )

    @patch('xmodule.video_module.transcripts_utils.requests.get', side_effect=mock_requests_get)
    def test_check_youtube_with_transcript_name(self, mock_get):
        """
        Test that the transcripts are fetched correctly when the the transcript name is set
        """
        self.set_fields_from_xml(self.item, '<video youtube="good_id_2" />')
        modulestore().update_item(self.item, self.user.id)

        subs = {
            'start': [100, 200, 240],
            'end': [200, 240, 380],
            'text': [
                'subs #1',
                'subs #2',
                'subs #3'
            ]
        }
        self.save_subs_to_store(subs, 'good_id_2')
        link = reverse('check_transcripts')
        data = {
            'locator': six.text_type(self.video_usage_key),
            'videos': [{
                'type': 'youtube',
                'video': 'good_id_2',
                'mode': 'youtube',
            }]
        }
        resp = self.client.get(link, {'data': json.dumps(data)})

        mock_get.assert_any_call(
            'http://video.google.com/timedtext',
            params={'lang': 'en', 'v': 'good_id_2', 'name': 'Custom'}
        )

        self.assertEqual(resp.status_code, 200)

        self.assertDictEqual(
            json.loads(resp.content.decode('utf-8')),
            {
                u'status': u'Success',
                u'youtube_local': True,
                u'is_youtube_mode': True,
                u'youtube_server': True,
                u'command': u'replace',
                u'current_item_subs': None,
                u'youtube_diff': True,
                u'html5_local': [],
                u'html5_equal': False,
            }
        )

    def test_fail_data_without_id(self):
        link = reverse('check_transcripts')
        data = {
            'locator': '',
            'videos': [{
                'type': '',
                'video': '',
                'mode': '',
            }]
        }
        resp = self.client.get(link, {'data': json.dumps(data)})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.content.decode('utf-8')).get('status'), "Can't find item by locator.")

    def test_fail_data_with_bad_locator(self):
        # Test for raising `InvalidLocationError` exception.
        link = reverse('check_transcripts')
        data = {
            'locator': '',
            'videos': [{
                'type': '',
                'video': '',
                'mode': '',
            }]
        }
        resp = self.client.get(link, {'data': json.dumps(data)})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.content.decode('utf-8')).get('status'), "Can't find item by locator.")

        # Test for raising `ItemNotFoundError` exception.
        data = {
            'locator': '{0}_{1}'.format(self.video_usage_key, 'BAD_LOCATOR'),
            'videos': [{
                'type': '',
                'video': '',
                'mode': '',
            }]
        }
        resp = self.client.get(link, {'data': json.dumps(data)})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.content.decode('utf-8')).get('status'), "Can't find item by locator.")

    def test_fail_for_non_video_module(self):
        # Not video module: setup
        data = {
            'parent_locator': six.text_type(self.course.location),
            'category': 'not_video',
            'type': 'not_video'
        }
        resp = self.client.ajax_post('/xblock/', data)
        usage_key = self._get_usage_key(resp)
        subs_id = str(uuid4())
        item = modulestore().get_item(usage_key)
        self.set_fields_from_xml(self.item, (u"""
            <not_video youtube="" sub="{}">
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.mp4"/>
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.webm"/>
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.ogv"/>
            </not_video>
        """.format(subs_id)))
        modulestore().update_item(item, self.user.id)

        subs = {
            'start': [100, 200, 240],
            'end': [200, 240, 380],
            'text': [
                'subs #1',
                'subs #2',
                'subs #3'
            ]
        }
        self.save_subs_to_store(subs, subs_id)

        data = {
            'locator': six.text_type(usage_key),
            'videos': [{
                'type': '',
                'video': '',
                'mode': '',
            }]
        }
        link = reverse('check_transcripts')
        resp = self.client.get(link, {'data': json.dumps(data)})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            json.loads(resp.content.decode('utf-8')).get('status'),
            'Transcripts are supported only for "video" modules.',
        )

    @patch('xmodule.video_module.transcripts_utils.get_video_transcript_content')
    def test_command_for_fallback_transcript(self, mock_get_video_transcript_content):
        """
        Verify the command if a transcript is there in edx-val.
        """
        mock_get_video_transcript_content.return_value = {
            'content': json.dumps({
                "start": [10],
                "end": [100],
                "text": ["Hi, welcome to Edx."],
            }),
            'file_name': 'edx.sjson'
        }

        # video_transcript_feature.return_value = feature_enabled
        self.set_fields_from_xml(self.item, (u"""
            <video youtube="" sub="" edx_video_id="123">
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.mp4"/>
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.webm"/>
                <source src="http://www.quirksmode.org/html5/videos/big_buck_bunny.ogv"/>
            </video>
        """))
        modulestore().update_item(self.item, self.user.id)

        # Make request to check transcript view
        data = {
            'locator': six.text_type(self.video_usage_key),
            'videos': [{
                'type': 'html5',
                'video': "",
                'mode': 'mp4',
            }]
        }
        check_transcripts_url = reverse('check_transcripts')
        response = self.client.get(check_transcripts_url, {'data': json.dumps(data)})

        # Assert the response
        self.assertEqual(response.status_code, 200)
        self.assertDictEqual(
            json.loads(response.content.decode('utf-8')),
            {
                u'status': u'Success',
                u'youtube_local': False,
                u'is_youtube_mode': False,
                u'youtube_server': False,
                u'command': 'found',
                u'current_item_subs': None,
                u'youtube_diff': True,
                u'html5_local': [],
                u'html5_equal': False,
            }
        )
