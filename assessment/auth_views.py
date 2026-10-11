
import hashlib
import hmac
import json
import logging
import re
import secrets

from datetime import timedelta
from smtplib import SMTPException
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import EmailOTP


logger = logging.getLogger(__name__)

OTP_LENGTH = 6
OTP_TTL = timedelta(minutes=10)
OTP_RESEND_COOLDOWN = timedelta(seconds=60)
OTP_MAX_ATTEMPTS = 5


# --------------------------------------------------
# OTP HELPERS
# --------------------------------------------------

def _otp_digest(user, otp):
    """Store an HMAC digest instead of the plain OTP."""
    secret = getattr(settings, 'EMAIL_OTP_HMAC_SECRET', '')

    if not secret:
        raise ImproperlyConfigured(
            'EMAIL_OTP_HMAC_SECRET must be configured.'
        )

    message = f'{user.pk}:{otp}'.encode('utf-8')

    return hmac.new(
        secret.encode('utf-8'),
        message,
        hashlib.sha256,
    ).hexdigest()


def _new_otp():
    """Generate a cryptographically secure six-digit OTP."""
    return f'{secrets.randbelow(10 ** OTP_LENGTH):06d}'


def _validated_email(value):
    """Validate and normalize an email address."""
    if not isinstance(value, str):
        return None

    email = value.strip().lower()

    try:
        validate_email(email)
    except ValidationError:
        return None

    return email


def _send_otp(email, otp):
    """Send the verification OTP using EmailJS."""

    service_id = getattr(settings, 'EMAILJS_SERVICE_ID', '')
    template_id = getattr(settings, 'EMAILJS_TEMPLATE_ID', '')
    public_key = getattr(settings, 'EMAILJS_PUBLIC_KEY', '')
    private_key = getattr(settings, 'EMAILJS_PRIVATE_KEY', '')

    if not all([
        service_id,
        template_id,
        public_key,
        private_key,
    ]):
        raise ImproperlyConfigured(
            'EmailJS environment variables are not configured.'
        )

    payload = {
        'service_id': service_id,
        'template_id': template_id,
        'user_id': public_key,
        'accessToken': private_key,
        'template_params': {
            'to_email': email,
            'otp': otp,
        },
    }

    request = Request(
        'https://api.emailjs.com/api/v1.0/email/send',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise SMTPException(
                    'EmailJS did not accept the verification email.'
                )

        return True

    except HTTPError as exc:
        error_body = exc.read().decode(
            'utf-8',
            errors='replace',
        )

        logger.error(
            'EmailJS failed: HTTP %s — %s',
            exc.code,
            error_body[:1000],
        )

        raise SMTPException(
            f'EmailJS failed with HTTP {exc.code}.'
        ) from exc

    except (URLError, TimeoutError, OSError) as exc:
        logger.error(
            'EmailJS connection failed: %s',
            type(exc).__name__,
        )

        raise SMTPException(
            'Could not connect to EmailJS.'
        ) from exc


def _user_to_dict(user):
    """Serialize user information for the frontend."""
    return {
        'id': user.id,
        'name': user.first_name or user.username,
        'email': user.email,
        'is_staff': user.is_staff,
        'date_joined': user.date_joined.isoformat(),
    }


# --------------------------------------------------
# REGISTER
# POST /api/auth/register/
# --------------------------------------------------

@api_view(['POST'])
@permission_classes([AllowAny])
def register(request):
    """Create an inactive account and email its OTP."""

    name = request.data.get('name', '')
    email = _validated_email(request.data.get('email'))
    password = request.data.get('password', '')

    if not isinstance(name, str):
        name = ''

    name = name.strip()

    if email is None or not isinstance(password, str) or not password:
        return Response(
            {'error': 'A valid email and password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < 6:
        return Response(
            {'error': 'Password must be at least 6 characters.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=email,
                email=email,
                password=password,
                first_name=name,
                is_active=False,
            )

            otp = _new_otp()
            now = timezone.now()

            EmailOTP.objects.create(
                user=user,
                otp_hash=_otp_digest(user, otp),
                expires_at=now + OTP_TTL,
                resend_available_at=now + OTP_RESEND_COOLDOWN,
            )

    except IntegrityError:
        return Response(
            {'error': 'An account with this email already exists.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    except ImproperlyConfigured:
        logger.exception('OTP configuration is missing.')

        return Response(
            {'error': 'Email verification is not configured correctly.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        _send_otp(email, otp)

    except (
        ImproperlyConfigured,
        SMTPException,
        OSError,
        ValueError,
    ):
        logger.exception('Failed to send registration OTP.')

        # Clean up the unverified account and its OTP.
        # Delete the OTP explicitly in case the model does
        # not cascade on user deletion.
        EmailOTP.objects.filter(user=user).delete()
        user.delete()

        return Response(
            {
                'error': (
                    'Verification email could not be sent. '
                    'Please try again later.'
                )
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        {
            'message': (
                'Registration successful. '
                'Please check your email for the verification code.'
            ),
            'email': email,
            'requires_verification': True,
        },
        status=status.HTTP_201_CREATED,
    )


# --------------------------------------------------
# VERIFY OTP
# POST /api/auth/verify-otp/
# --------------------------------------------------

@api_view(['POST'])
@permission_classes([AllowAny])
def verify_otp(request):
    """Verify OTP, activate the account and create a session."""

    email = _validated_email(request.data.get('email'))
    otp = request.data.get('otp')

    if email is None:
        return Response(
            {'error': 'Enter a valid email address.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if (
        not isinstance(otp, str)
        or re.fullmatch(r'[0-9]{6}', otp) is None
    ):
        return Response(
            {'error': 'Enter the 6-digit verification code.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = User.objects.filter(email__iexact=email).first()

    if user is None:
        return Response(
            {'error': 'Invalid or expired verification code.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)

        if user.is_active:
            return Response(
                {'message': 'This email is already verified.'},
                status=status.HTTP_200_OK,
            )

        otp_record = (
            EmailOTP.objects
            .select_for_update()
            .filter(user=user)
            .first()
        )

        if otp_record is None:
            return Response(
                {
                    'error': (
                        'No active verification code. '
                        'Request a new one.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()

        if otp_record.expires_at <= now:
            otp_record.delete()

            return Response(
                {
                    'error': (
                        'Verification code expired. '
                        'Request a new one.'
                    ),
                    'expired': True,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if otp_record.attempts >= OTP_MAX_ATTEMPTS:
            return Response(
                {
                    'error': (
                        'Too many incorrect codes. '
                        'Request a new one.'
                    ),
                    'attempts_remaining': 0,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        expected_digest = _otp_digest(user, otp)

        if not hmac.compare_digest(
            otp_record.otp_hash,
            expected_digest,
        ):
            otp_record.attempts += 1
            otp_record.save(update_fields=['attempts'])

            remaining = OTP_MAX_ATTEMPTS - otp_record.attempts

            message = (
                'Invalid verification code.'
                if remaining > 0
                else (
                    'Too many incorrect codes. '
                    'Request a new one.'
                )
            )

            response_status = (
                status.HTTP_400_BAD_REQUEST
                if remaining > 0
                else status.HTTP_429_TOO_MANY_REQUESTS
            )

            return Response(
                {
                    'error': message,
                    'attempts_remaining': remaining,
                },
                status=response_status,
            )

        user.is_active = True
        user.save(update_fields=['is_active'])

        otp_record.delete()

    # Create the authenticated Django session.
    login(request, user)

    return Response(
        {
            'message': 'Email verified successfully.',
            'user': _user_to_dict(user),
        },
        status=status.HTTP_200_OK,
    )


# --------------------------------------------------
# RESEND OTP
# POST /api/auth/resend-otp/
# --------------------------------------------------

@api_view(['POST'])
@permission_classes([AllowAny])
def resend_otp(request):
    """Generate and send a replacement OTP."""

    email = _validated_email(request.data.get('email'))

    if email is None:
        return Response(
            {'error': 'Enter a valid email address.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = User.objects.filter(email__iexact=email).first()

    if user is None:
        return Response(
            {'error': 'Account not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if user.is_active:
        return Response(
            {
                'message': 'This email is already verified.',
                'already_verified': True,
            },
            status=status.HTTP_200_OK,
        )

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=user.pk)

            if user.is_active:
                return Response(
                    {
                        'message': 'This email is already verified.',
                        'already_verified': True,
                    },
                    status=status.HTTP_200_OK,
                )

            otp_record = (
                EmailOTP.objects
                .select_for_update()
                .filter(user=user)
                .first()
            )

            now = timezone.now()

            if (
                otp_record
                and now < otp_record.resend_available_at
            ):
                retry_after = max(
                    1,
                    int(
                        (
                            otp_record.resend_available_at - now
                        ).total_seconds()
                    ),
                )

                return Response(
                    {
                        'error': (
                            'Please wait before requesting '
                            'another code.'
                        ),
                        'retry_after': retry_after,
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            otp = _new_otp()

            EmailOTP.objects.update_or_create(
                user=user,
                defaults={
                    'otp_hash': _otp_digest(user, otp),
                    'expires_at': now + OTP_TTL,
                    'attempts': 0,
                    'resend_available_at': (
                        now + OTP_RESEND_COOLDOWN
                    ),
                },
            )

    except ImproperlyConfigured:
        logger.exception('OTP configuration is missing.')

        return Response(
            {'error': 'Email verification is not configured correctly.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        _send_otp(email, otp)

    except (
        ImproperlyConfigured,
        SMTPException,
        OSError,
        ValueError,
    ):
        logger.exception('Failed to resend OTP.')

        # Invalidate the code if delivery fails so the user
        # cannot be left relying on an undelivered replacement.
        EmailOTP.objects.filter(user=user).delete()

        return Response(
            {
                'error': (
                    'Verification email could not be sent. '
                    'Please try again later.'
                )
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        {
            'message': 'A new verification code was sent.',
            'resend_available_in': 60,
        },
        status=status.HTTP_200_OK,
    )


# --------------------------------------------------
# LOGIN
# POST /api/auth/login/
# --------------------------------------------------

@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """Authenticate using email and password."""

    email = _validated_email(request.data.get('email'))
    password = request.data.get('password', '')

    if email is None or not isinstance(password, str) or not password:
        return Response(
            {'error': 'Email and password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Email is also used as the Django username.
    user = authenticate(
        request,
        username=email,
        password=password,
    )

    if user is None:
        unverified_user = User.objects.filter(
            username=email,
            is_active=False,
        ).first()

        if (
            unverified_user
            and unverified_user.check_password(password)
        ):
            return Response(
                {
                    'error': 'Verify your email before signing in.',
                    'requires_verification': True,
                    'email': unverified_user.email,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(
            {'error': 'Invalid email or password.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    login(request, user)

    return Response(
        {
            'message': 'Login successful.',
            'user': _user_to_dict(user),
        },
        status=status.HTTP_200_OK,
    )


# --------------------------------------------------
# LOGOUT
# POST /api/auth/logout/
# --------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    """Clear the authenticated Django session."""

    logout(request)

    return Response(
        {'message': 'Logged out successfully.'},
        status=status.HTTP_200_OK,
    )


# --------------------------------------------------
# CURRENT USER
# GET /api/auth/me/
# --------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    """Return the currently authenticated user's information."""

    return Response(
        {
            'user': _user_to_dict(request.user),
        },
        status=status.HTTP_200_OK,
    )


# --------------------------------------------------
# CSRF TOKEN
# GET /api/auth/csrf/
# --------------------------------------------------

@api_view(['GET'])
@permission_classes([AllowAny])
def csrf_token(request):
    """Return a CSRF token for frontend requests."""

    return JsonResponse(
        {
            'csrfToken': get_token(request),
        }
    )
