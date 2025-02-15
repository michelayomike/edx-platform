"""
Common utilities for the course experience, including course outline.
"""
from __future__ import absolute_import

from completion.models import BlockCompletion
from django.utils.translation import ugettext as _
from opaque_keys.edx.keys import CourseKey
from six.moves import range
from web_fragments.fragment import Fragment

from lms.djangoapps.course_api.blocks.api import get_blocks
from lms.djangoapps.course_blocks.utils import get_student_module_as_dict
from lms.djangoapps.courseware.date_summary import verified_upgrade_deadline_link
from openedx.core.djangolib.markup import HTML
from openedx.core.lib.cache_utils import request_cached
from openedx.features.discounts.applicability import (
    can_receive_discount,
    get_discount_expiration_date,
    discount_percentage
)
from openedx.features.discounts.utils import format_strikeout_price
from xmodule.modulestore.django import modulestore


@request_cached()
def get_course_outline_block_tree(request, course_id, user=None):
    """
    Returns the root block of the course outline, with children as blocks.
    """

    assert user is None or user.is_authenticated

    def populate_children(block, all_blocks):
        """
        Replace each child id with the full block for the child.

        Given a block, replaces each id in its children array with the full
        representation of that child, which will be looked up by id in the
        passed all_blocks dict. Recursively do the same replacement for children
        of those children.
        """
        children = block.get('children', [])

        for i in range(len(children)):
            child_id = block['children'][i]
            child_detail = populate_children(all_blocks[child_id], all_blocks)
            block['children'][i] = child_detail

        return block

    def set_last_accessed_default(block):
        """
        Set default of False for resume_block on all blocks.
        """
        block['resume_block'] = False
        block['complete'] = False
        for child in block.get('children', []):
            set_last_accessed_default(child)

    def mark_blocks_completed(block, user, course_key):
        """
        Walk course tree, marking block completion.
        Mark 'most recent completed block as 'resume_block'

        """
        last_completed_child_position = BlockCompletion.get_latest_block_completed(user, course_key)

        if last_completed_child_position:
            # Mutex w/ NOT 'course_block_completions'
            recurse_mark_complete(
                course_block_completions=BlockCompletion.get_course_completions(user, course_key),
                latest_completion=last_completed_child_position,
                block=block
            )

    def recurse_mark_complete(course_block_completions, latest_completion, block):
        """
        Helper function to walk course tree dict,
        marking blocks as 'complete' and 'last_complete'

        If all blocks are complete, mark parent block complete
        mark parent blocks of 'last_complete' as 'last_complete'

        :param course_block_completions: dict[course_completion_object] =  completion_value
        :param latest_completion: course_completion_object
        :param block: course_outline_root_block block object or child block

        :return:
            block: course_outline_root_block block object or child block
        """
        block_key = block.serializer.instance

        if course_block_completions.get(block_key):
            block['complete'] = True
            if block_key == latest_completion.full_block_key:
                block['resume_block'] = True

        if block.get('children'):
            for idx in range(len(block['children'])):
                recurse_mark_complete(
                    course_block_completions,
                    latest_completion,
                    block=block['children'][idx]
                )
                if block['children'][idx].get('resume_block') is True:
                    block['resume_block'] = True

            completable_blocks = [child for child in block['children']
                                  if child.get('type') != 'discussion']
            if all(child.get('complete') for child in completable_blocks):
                block['complete'] = True

    def mark_last_accessed(user, course_key, block):
        """
        Recursively marks the branch to the last accessed block.
        """
        block_key = block.serializer.instance
        student_module_dict = get_student_module_as_dict(user, course_key, block_key)

        last_accessed_child_position = student_module_dict.get('position')
        if last_accessed_child_position and block.get('children'):
            block['resume_block'] = True
            if last_accessed_child_position <= len(block['children']):
                last_accessed_child_block = block['children'][last_accessed_child_position - 1]
                last_accessed_child_block['resume_block'] = True
                mark_last_accessed(user, course_key, last_accessed_child_block)
            else:
                # We should be using an id in place of position for last accessed.
                # However, while using position, if the child block is no longer accessible
                # we'll use the last child.
                block['children'][-1]['resume_block'] = True

    course_key = CourseKey.from_string(course_id)
    course_usage_key = modulestore().make_course_usage_key(course_key)

    # Deeper query for course tree traversing/marking complete
    # and last completed block
    block_types_filter = [
        'course',
        'chapter',
        'sequential',
        'vertical',
        'html',
        'problem',
        'video',
        'discussion',
        'drag-and-drop-v2',
        'poll',
        'word_cloud'
    ]
    all_blocks = get_blocks(
        request,
        course_usage_key,
        user=request.user,
        nav_depth=3,
        requested_fields=[
            'children',
            'display_name',
            'type',
            'due',
            'graded',
            'special_exam_info',
            'show_gated_sections',
            'format'
        ],
        block_types_filter=block_types_filter
    )

    course_outline_root_block = all_blocks['blocks'].get(all_blocks['root'], None)
    if course_outline_root_block:
        populate_children(course_outline_root_block, all_blocks['blocks'])
        if user:
            set_last_accessed_default(course_outline_root_block)
            mark_blocks_completed(
                block=course_outline_root_block,
                user=request.user,
                course_key=course_key
            )
    return course_outline_root_block


def get_resume_block(block):
    """
    Gets the deepest block marked as 'resume_block'.

    """
    if block.get('authorization_denial_reason') or not block['resume_block']:
        return None
    if not block.get('children'):
        return block

    for child in block['children']:
        resume_block = get_resume_block(child)
        if resume_block:
            return resume_block
    return block


def get_first_purchase_offer_banner_fragment(user, course):
    if user and course:
        discount_expiration_date = get_discount_expiration_date(user, course)
        if (discount_expiration_date and
                can_receive_discount(user=user, course=course, discount_expiration_date=discount_expiration_date)):
            # Translator: xgettext:no-python-format
            offer_message = _(u'{banner_open} Upgrade by {discount_expiration_date} and save {percentage}% '
                              u'[{strikeout_price}]{span_close}{br}Discount will be automatically applied at checkout. '
                              u'{a_open}Upgrade Now{a_close}{div_close}')
            return Fragment(HTML(offer_message).format(
                a_open=HTML(u'<a href="{upgrade_link}">').format(
                    upgrade_link=verified_upgrade_deadline_link(user=user, course=course)
                ),
                a_close=HTML('</a>'),
                br=HTML('<br>'),
                banner_open=HTML(
                    '<div class="first-purchase-offer-banner"><span class="first-purchase-offer-banner-bold">'
                ),
                discount_expiration_date=discount_expiration_date.strftime(u'%B %d'),
                percentage=discount_percentage(),
                span_close=HTML('</span>'),
                div_close=HTML('</div>'),
                strikeout_price=HTML(format_strikeout_price(user, course, check_for_discount=False)[0])
            ))
    return None
