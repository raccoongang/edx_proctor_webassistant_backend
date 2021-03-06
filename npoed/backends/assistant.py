"""
Integration with Proctor webassistant's proctoring system
"""

from Crypto.Cipher import DES3
import base64
from hashlib import sha256
import requests
import hmac
import binascii
import datetime
import json
import logging

from django.conf import settings
from django.contrib.auth.models import User

from edx_proctoring.backends.backend import ProctoringBackendProvider
from edx_proctoring import constants
from edx_proctoring.exceptions import (
    BackendProvideCannotRegisterAttempt,
    StudentExamAttemptDoesNotExistsException,
    ProctoredExamSuspiciousLookup,
    ProctoredExamReviewAlreadyExists,
    ProctoredExamBadReviewStatus,
)
from edx_proctoring.utils import locate_attempt_by_attempt_code
from edx_proctoring.models import (
    ProctoredExamSoftwareSecureComment,
    ProctoredExamSoftwareSecureReview,
    ProctoredExamStudentAttemptStatus,
)

log = logging.getLogger(__name__)


class NPOEDBackendProvider(ProctoringBackendProvider):
    """
    Implementation of the ProctoringBackendProvider for Proctor webassistant's
    RPNow product
    """

    def __init__(self, organization, exam_sponsor, exam_register_endpoint,
                 secret_key_id, secret_key, crypto_key, software_download_url):
        """
        Class initializer
        """

        self.organization = organization
        self.exam_sponsor = exam_sponsor
        self.exam_register_endpoint = exam_register_endpoint
        self.secret_key_id = secret_key_id
        self.secret_key = secret_key
        self.crypto_key = crypto_key
        self.timeout = 10
        self.software_download_url = software_download_url
        self.passing_review_status = ['Clean', 'Rules Violation']
        self.failing_review_status = ['Not Reviewed', 'Suspicious']

    def register_exam_attempt(self, exam, context):
        """
        Method that is responsible for communicating with the backend provider
        to establish a new proctored exam
        """

        attempt_code = context['attempt_code']

        data = self._get_payload(
            exam,
            context
        )
        headers = {
            "Content-Type": 'application/json'
        }
        http_date = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
        signature = self._sign_doc(data, 'POST', headers, http_date)

        status, response = self._send_request_to_ssi(data, signature, http_date)

        if status not in [200, 201]:
            err_msg = (
                u'Could not register attempt_code = {attempt_code}. '
                'HTTP Status code was {status_code} and response was {response}.'.format(
                    attempt_code=attempt_code,
                    status_code=status,
                    response=response
                )
            )
            log.error(err_msg)
            raise BackendProvideCannotRegisterAttempt(err_msg)

        # get the external ID that Proctor webassistant has defined
        # for this attempt
        ssi_record_locator = json.loads(response)['ID']

        return ssi_record_locator

    def start_exam_attempt(self, exam, attempt):  # pylint: disable=unused-argument
        """
        Called when the exam attempt has been created but not started
        """
        return None

    def stop_exam_attempt(self, exam, attempt):
        """
        Method that is responsible for communicating with the backend provider
        to establish a new proctored exam
        """
        return None

    def get_software_download_url(self):
        """
        Returns the URL that the user needs to go to in order to download
        the corresponding desktop software
        """
        return self.software_download_url

    def on_review_callback(self, payload):
        """
        Called when the reviewing 3rd party service posts back the results
        Documentation on the data format can be found from ProctorWebassistant's
        documentation named "Reviewer Data Transfer"
        """

        log_msg = (
            'Received callback from ProctorWebassistant with review data: {payload}'.format(
                payload=payload
            )
        )
        log.info(log_msg)

        # what we consider the external_id is ProctorWebassistant's 'ssiRecordLocator'
        external_id = payload['examMetaData']['ssiRecordLocator']

        # what we consider the attempt_code is ProctorWebassistant's 'examCode'
        attempt_code = payload['examMetaData']['examCode']

        # get the ProctorWebassistant status on this attempt
        review_status = payload['reviewStatus']

        bad_status = review_status not in self.passing_review_status + self.failing_review_status

        if bad_status:
            err_msg = (
                'Received unexpected reviewStatus field calue from payload. '
                'Was {review_status}.'.format(review_status=review_status)
            )
            raise ProctoredExamBadReviewStatus(err_msg)

        # do a lookup on the attempt by examCode, and compare the
        # passed in ssiRecordLocator and make sure it matches
        # what we recorded as the external_id. We need to look in both
        # the attempt table as well as the archive table

        (attempt_obj, is_archived_attempt) = locate_attempt_by_attempt_code(attempt_code)
        if not attempt_obj:
            # still can't find, error out
            err_msg = (
                'Could not locate attempt_code: {attempt_code}'.format(attempt_code=attempt_code)
            )
            raise StudentExamAttemptDoesNotExistsException(err_msg)

        # then make sure we have the right external_id
        # note that ProctorWebassistant might send a case insensitive
        # ssiRecordLocator than what it returned when we registered the
        # exam
        match = (
            attempt_obj.external_id.lower() == external_id.lower() or
            settings.PROCTORING_SETTINGS.get('ALLOW_CALLBACK_SIMULATION', False)
        )
        if not match:
            err_msg = (
                'Found attempt_code {attempt_code}, but the recorded external_id did not '
                'match the ssiRecordLocator that had been recorded previously. Has {existing} '
                'but received {received}!'.format(
                    attempt_code=attempt_code,
                    existing=attempt_obj.external_id,
                    received=external_id
                )
            )
            raise ProctoredExamSuspiciousLookup(err_msg)

        # do some limited parsing of the JSON payload
        review_status = payload['reviewStatus']
        video_review_link = payload['videoReviewLink']

        # do we already have a review for this attempt?!? We may not allow updates
        review = ProctoredExamSoftwareSecureReview.get_review_by_attempt_code(attempt_code)

        if review:
            if not settings.PROCTORING_SETTINGS.get('ALLOW_REVIEW_UPDATES', True):
                err_msg = (
                    'We already have a review submitted from ProctorWebassistant regarding '
                    'attempt_code {attempt_code}. We do not allow for updates!'.format(
                        attempt_code=attempt_code
                    )
                )
                raise ProctoredExamReviewAlreadyExists(err_msg)

            # we allow updates
            warn_msg = (
                'We already have a review submitted from ProctorWebassistant regarding '
                'attempt_code {attempt_code}. We have been configured to allow for '
                'updates and will continue...'.format(
                    attempt_code=attempt_code
                )
            )
            log.warn(warn_msg)
        else:
            # this is first time we've received this attempt_code, so
            # make a new record in the review table
            review = ProctoredExamSoftwareSecureReview()

        review.attempt_code = attempt_code
        review.raw_data = json.dumps(payload)
        review.review_status = review_status
        review.video_url = video_review_link
        review.student = attempt_obj.user
        review.exam = attempt_obj.proctored_exam
        # set reviewed_by to None because it was reviewed by our 3rd party
        # service provider, not a user in our database
        try:
            reviewer_username = payload['examMetaData']['proctor_username']
            reviewer = User.objects.get(username=reviewer_username)
        except (User.DoesNotExist, KeyError):
            reviewer = None
        review.reviewed_by = reviewer

        review.save()

        # go through and populate all of the specific comments
        for comment in payload.get('webCamComments', []):
            self._save_review_comment(review, comment)

        for comment in payload.get('desktopComments', []):
            self._save_review_comment(review, comment)

        # we could have gotten a review for an archived attempt
        # this should *not* cause an update in our credit
        # eligibility table
        if not is_archived_attempt:
            allow_status_update_on_fail = not settings.PROCTORING_SETTINGS.get(
                'REQUIRE_FAILURE_SECOND_REVIEWS', False
            )
            self.on_review_saved(review, allow_status_update_on_fail=allow_status_update_on_fail)

    def on_review_saved(self, review, allow_status_update_on_fail=False):  # pylint: disable=arguments-differ
        """
        called when a review has been save - either through API (on_review_callback) or via Django Admin panel
        in order to trigger any workflow associated with proctoring review results
        """

        (attempt_obj, is_archived_attempt) = locate_attempt_by_attempt_code(review.attempt_code)

        if not attempt_obj:
            # This should not happen, but it is logged in the help
            # method
            return

        if is_archived_attempt:
            # we don't trigger workflow on reviews on archived attempts
            err_msg = (
                'Got on_review_save() callback for an archived attempt with '
                'attempt_code {attempt_code}. Will not trigger workflow...'.format(
                    attempt_code=review.attempt_code
                )
            )
            log.warn(err_msg)
            return

        # only 'Clean' and 'Rules Violation' count as passing
        status = (
            ProctoredExamStudentAttemptStatus.verified
            if review.review_status in self.passing_review_status
            else ProctoredExamStudentAttemptStatus.rejected
        )

        # are we allowed to update the status if we have a failure status
        # i.e. do we need a review to come in from Django Admin panel?
        if status == ProctoredExamStudentAttemptStatus.verified or allow_status_update_on_fail:
            # updating attempt status will trigger workflow
            # (i.e. updating credit eligibility table)
            from edx_proctoring.api import update_attempt_status

            update_attempt_status(
                attempt_obj.proctored_exam_id,
                attempt_obj.user_id,
                status
            )

    def _save_review_comment(self, review, comment):
        """
        Helper method to save a review comment
        """
        comment = ProctoredExamSoftwareSecureComment(
            review=review,
            start_time=comment['eventStart'],
            stop_time=comment['eventFinish'],
            duration=comment['duration'],
            comment=comment['comments'],
            status=comment['eventStatus']
        )
        comment.save()

    def _encrypt_password(self, key, pwd):
        """
        Encrypt the exam passwork with the given key
        """
        block_size = DES3.block_size

        def pad(text):
            """
            Apply padding
            """
            return text + (block_size - len(text) % block_size) * chr(block_size - len(text) % block_size)
        cipher = DES3.new(key, DES3.MODE_ECB)
        encrypted_text = cipher.encrypt(pad(pwd))
        return base64.b64encode(encrypted_text)

    def _split_fullname(self, full_name):
        """
        Utility to break Full Name to first and last name
        """
        first_name = ''
        last_name = ''
        name_elements = full_name.split(' ')
        first_name = name_elements[0]
        if len(name_elements) > 1:
            last_name = ' '.join(name_elements[1:])

        return (first_name, last_name)

    def _get_payload(self, exam, context):
        """
        Constructs the data payload that Proctor webassistant expects
        """

        attempt_code = context['attempt_code']
        time_limit_mins = context['time_limit_mins']
        is_sample_attempt = context['is_sample_attempt']
        callback_url = context['callback_url']
        full_name = context['full_name']
        review_policy = context.get('review_policy', "")
        review_policy_exception = context.get('review_policy_exception')

        # compile the notes to the reviewer
        # this is a combination of the Exam Policy which is for all students
        # combined with any exceptions granted to the particular student
        reviewer_notes = review_policy
        if review_policy_exception:
            reviewer_notes = '{notes}; {exception}'.format(
                notes=reviewer_notes,
                exception=review_policy_exception
            )

        (first_name, last_name) = self._split_fullname(full_name)

        now = datetime.datetime.utcnow()
        start_time_str = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
        end_time_str = (now + datetime.timedelta(minutes=time_limit_mins)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return {
            "examCode": attempt_code,
            "organization": self.organization,
            "duration": time_limit_mins,
            "reviewedExam": not is_sample_attempt,
            # NOTE: we will have to allow these notes to be authorable in Studio
            # and then we will pull this from the exam database model
            "reviewerNotes": reviewer_notes,
            "examPassword": self._encrypt_password(self.crypto_key, attempt_code),
            "examSponsor": self.exam_sponsor,
            "examName": exam['exam_name'],
            "ssiProduct": 'rp-now',
            # need to pass in a URL to the LMS?
            "examUrl": callback_url,
            "orgExtra": {
                "examStartDate": start_time_str,
                "examEndDate": end_time_str,
                "noOfStudents": 1,
                "examID": exam['id'],
                "courseID": exam['course_id'],
                "firstName": first_name,
                "lastName": last_name,
                "userID": context.get('user_id'),
                "username": context.get('username'),
                "email": context.get('email')
            }
        }

    def _header_string(self, headers, date):
        """
        Composes the HTTP header string that ProctorWebassistant expects
        """
        # Headers
        string = ""
        if 'Content-Type' in headers:
            string += headers.get('Content-Type')
            string += '\n'

        if date:
            string += date
            string += '\n'

        return string

    def _body_string(self, body_json, prefix=""):
        """
        Serializes out the HTTP body that ProctorWebassistant expects
        """
        keys = body_json.keys()
        keys.sort()
        string = ""
        for key in keys:
            value = body_json[key]
            if isinstance(value, bool):
                if value:
                    value = 'true'
                else:
                    value = 'false'
            if isinstance(value, (list, tuple)):
                for idx, arr in enumerate(value):
                    if isinstance(arr, dict):
                        string += self._body_string(arr, key + '.' + str(idx) + '.')
                    else:
                        string += key + '.' + str(idx) + ':' + arr + '\n'
            elif isinstance(value, dict):
                string += self._body_string(value, key + '.')
            else:
                if value != "" and not value:
                    value = "null"
                string += str(prefix) + str(key) + ":" + unicode(value).encode('utf-8') + '\n'

        return string

    def _sign_doc(self, body_json, method, headers, date):
        """
        Digitaly signs the datapayload that ProctorWebassistant expects
        """
        body_str = self._body_string(body_json)

        method_string = method + '\n\n'

        headers_str = self._header_string(headers, date)
        message = method_string + headers_str + body_str

        # HMAC requires a string not a unicode
        message = str(message)

        log_msg = (
            'About to send payload to ProctorWebassistant:\n{message}'.format(message=message)
        )
        log.info(log_msg)

        hashed = hmac.new(str(self.secret_key), str(message), sha256)
        computed = binascii.b2a_base64(hashed.digest()).rstrip('\n')

        return 'SSI ' + self.secret_key_id + ':' + computed

    def _send_request_to_ssi(self, data, sig, date):
        """
        Performs the webservice call to ProctorWebassistant
        """
        response = requests.post(
            self.exam_register_endpoint,
            headers={
                'Content-Type': 'application/json',
                "Authorization": sig,
                "Date": date
            },
            data=json.dumps(data),
            timeout=self.timeout
        )

        return response.status_code, response.text
