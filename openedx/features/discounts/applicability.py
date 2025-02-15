# -*- coding: utf-8 -*-
"""
Contains code related to computing discount percentage
and discount applicability.

WARNING:
Keep in mind that the code in this file only applies to discounts controlled in the lms like the first purchase offer,
not other discounts like coupons or enterprise/program offers configured in ecommerce.

"""
from __future__ import absolute_import

from datetime import datetime, timedelta

from crum import get_current_request, impersonate
from django.utils import timezone
import pytz

from course_modes.models import CourseMode
from entitlements.models import CourseEntitlement
from lms.djangoapps.experiments.stable_bucketing import stable_bucketing_hash_group
from openedx.core.djangoapps.waffle_utils import WaffleFlag, WaffleFlagNamespace
from openedx.features.discounts.models import DiscountRestrictionConfig
from student.models import CourseEnrollment
from track import segment

# .. feature_toggle_name: discounts.enable_discounting
# .. feature_toggle_type: flag
# .. feature_toggle_default: False
# .. feature_toggle_description: Toggle discounts always being disabled
# .. feature_toggle_category: discounts
# .. feature_toggle_use_cases: monitored_rollout
# .. feature_toggle_creation_date: 2019-4-16
# .. feature_toggle_expiration_date: None
# .. feature_toggle_warnings: None
# .. feature_toggle_tickets: REVEM-282
# .. feature_toggle_status: supported
DISCOUNT_APPLICABILITY_FLAG = WaffleFlag(
    waffle_namespace=WaffleFlagNamespace(name=u'discounts'),
    flag_name=u'enable_discounting',
    flag_undefined_default=False
)

DISCOUNT_APPLICABILITY_HOLDBACK = 'first_purchase_discount_holdback'


def get_discount_expiration_date(user, course):
    """
    Returns the date when the discount expires for the user.
    Returns none if the user is not enrolled.
    """
    course_enrollment = CourseEnrollment.objects.filter(
        user=user,
        course=course.id,
        mode__in=CourseMode.UPSELL_TO_VERIFIED_MODES
    )
    if len(course_enrollment) != 1:
        return None

    enrollment = course_enrollment.first()
    try:
        # Content availability date is equivalent to max(enrollment date, course start date)
        # for most people. Using the schedule date will provide flexibility to deal with
        # more complex business rules in the future.
        content_availability_date = enrollment.schedule.start
        # We have anecdotally observed a case where the schedule.start was
        # equal to the course start, but should have been equal to the enrollment start
        # https://openedx.atlassian.net/browse/PROD-58
        # This section is meant to address that case
        if enrollment.created and course.start:
            if (content_availability_date.date() == course.start.date() and
               course.start < enrollment.created < timezone.now()):
                content_availability_date = enrollment.created
    except CourseEnrollment.schedule.RelatedObjectDoesNotExist:
        content_availability_date = max(enrollment.created, course.start)

    return content_availability_date + timedelta(weeks=1)


def can_receive_discount(user, course, discount_expiration_date=None):
    """
    Check all the business logic about whether this combination of user and course
    can receive a discount.
    """
    # Always disable discounts until we are ready to enable this feature
    with impersonate(user):
        if not DISCOUNT_APPLICABILITY_FLAG.is_enabled():
            return False

    # TODO: Add additional conditions to return False here

    # Check if discount has expired
    if not discount_expiration_date:
        discount_expiration_date = get_discount_expiration_date(user, course)

    if discount_expiration_date is None:
        return False

    if discount_expiration_date < timezone.now():
        return False

    # Course end date needs to be in the future
    if course.has_ended():
        return False

    # Course needs to have a non-expired verified mode
    modes_dict = CourseMode.modes_for_course_dict(course=course, include_expired=False)
    verified_mode = modes_dict.get('verified', None)
    if not verified_mode:
        return False

    # Site, Partner, Course or Course Run not excluded from lms-controlled discounts
    if DiscountRestrictionConfig.disabled_for_course_stacked_config(course):
        return False

    if user.is_anonymous:
        return False

    # Don't allow users who have enrolled in any courses in non-upsellable
    # modes
    if CourseEnrollment.objects.filter(user=user).exclude(mode__in=CourseMode.UPSELL_TO_VERIFIED_MODES).exists():
        return False

    # Don't allow any users who have entitlements (past or present)
    if CourseEntitlement.objects.filter(user=user).exists():
        return False

    # We can't import this at Django load time within the openedx tests settings context
    from openedx.features.enterprise_support.utils import is_enterprise_learner
    # Don't give discount to enterprise users
    if is_enterprise_learner(user):
        return False

    # Excute holdback
    if _is_in_holdback(user):
        return False

    return True


def _is_in_holdback(user):
    """
    Return whether the specified user is in the first-purchase-discount holdback group.
    """
    if datetime(2020, 8, 1, tzinfo=pytz.UTC) <= datetime.now(tz=pytz.UTC):
        return False

    # Holdback is 50/50
    bucket = stable_bucketing_hash_group(DISCOUNT_APPLICABILITY_HOLDBACK, 2, user.username)

    request = get_current_request()
    if hasattr(request, 'session') and DISCOUNT_APPLICABILITY_HOLDBACK not in request.session:

        properties = {
            'site': request.site.domain,
            'app_label': 'discounts',
            'nonInteraction': 1,
            'bucket': bucket,
            'experiment': 'REVEM-363',
        }
        segment.track(
            user_id=user.id,
            event_name='edx.bi.experiment.user.bucketed',
            properties=properties,
        )

        # Mark that we've recorded this bucketing, so that we don't do it again this session
        request.session[DISCOUNT_APPLICABILITY_HOLDBACK] = True

    return bucket == 0


def discount_percentage():
    """
    Get the configured discount amount.
    """
    # TODO: Add configuration information here
    return 15
