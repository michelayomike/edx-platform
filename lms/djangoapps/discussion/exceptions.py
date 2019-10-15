"""
Custom exceptions raised by Discussion API.
"""

class UserNotPermittedException(BaseException):
    """
    This is the exception raised when a user is not
    permitted to view the discussion thread
    """
    pass
