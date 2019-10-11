from lms.djangoapps.teams.models import CourseTeam


def get_team_by_discussion(discussion_id):
    """
    This is a function to get team object by the discussion_id passed in.
    If the discussion_id is not associated with any team, we return None
    """
    try:
        return CourseTeam.objects.get(discussion_topic_id=discussion_id)
    except CourseTeam.DoesNotExist:
        # When the discussion does not belong to a team. It's visible in
        # any team context
        return None


def is_team_discussion_private(team, discussion_id):
    """
    This is the function to check if the team is configured to have its discussion
    to be private. We need a way to check the setting on the team.
    This function also provide ways to toggle the setting of discussion visibility on the
    individual team level.
    """
    if team:
        # check visibility setting on the team
        return True
    else:
        return False


def user_is_a_team_member(user, team):
    """
    Return if the user is a member of the team
    If the team is not defined, return False
    """
    if team:
        return team.users.filter(id=user.id).exists()
    return False


def discussion_visibile_by_user(discussion_id, user):
    """
    This function checks whether the discussion should be visible to the user.
    The discussion should not be visible to the user if
    * The discussion is part of the Team AND
    * The team is configured to hide the discussions from non-teammembers AND
    * The user is not part of the team
    """
    team = get_team_by_discussion(discussion_id)
    return not is_team_discussion_private(team, discussion_id) or user_is_a_team_member(user, team)
