from django.middleware.csrf import get_token
from django.http import JsonResponse

import logging

logger = logging.getLogger(__name__)

"""
Authentication API views using Django's built-in User model.

Endpoints:
    POST /api/auth/register/  — create an inactive account and email a code
    POST /api/auth/verify-otp/ — verify email and create a session
    POST /api/auth/resend-otp/ — send a replacement verification code
  POST /api/auth/login/     — email + password → session cookie
  POST /api/auth/logout/    — clear session
  GET  /api/auth/me/        — return current user info (or 401)
"""

import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from smtplib import SMTPException

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import EmailOTP


OTP_LENGTH = 6
OTP_TTL = timedelta(minutes=10)
OTP_RESEND_COOLDOWN = timedelta(seconds=60)
OTP_MAX_ATTEMPTS = 5


def _otp_digest(user, otp):
    secret = settings.EMAIL_OTP_HMAC_SECRET
    if not secret:
        raise ImproperlyConfigured('EMAIL_OTP_HMAC_SECRET must be configured.')
    message = f'{user.pk}:{otp}'.encode('utf-8')
    return hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()


def _new_otp():
    return f'{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}'



import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError



    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "EmailJS failed: HTTP %s — %s",
            exc.code,
            error_body[:1000],
        )
        raise SMTPException(
            f"EmailJS failed with HTTP {exc.code}."
        ) from exc

    except (URLError, TimeoutError) as exc:
        logger.error("EmailJS connection failed: %s", type(exc).__name__)
        raise SMTPException(
            "Could not connect to EmailJS."
        ) from exc

    payload = {
        "service_id": service_id,
        "template_id": template_id,
        "user_id": public_key,
        "accessToken": private_key,
        "template_params": {
            "to_email": email,
            "otp": otp,
        },
    }

    request = Request(
        "https://api.emailjs.com/api/v1.0/email/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise SMTPException(
                    "Verification email was not accepted for delivery."
                )
        return 1

    except (HTTPError, URLError, TimeoutError) as exc:
        raise SMTPException(
            "EmailJS could not send the verification email."
        ) from exc

def _validated_email(value):
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return None
    return email


@api_view(['POST'])
@permission_classes([AllowAny])
def register(request):
    """Create an inactive account and send its first verification code."""
    name = request.data.get('name', '').strip()
    email = request.data.get('email', '').strip().lower()
    password = request.data.get('password', '')

    # Validation
    if not email or not password:
        return Response(
            {'error': 'Email and password are required.'},
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
            _send_otp(email, otp)
    except IntegrityError:
        return Response(
            {'error': 'An account with this email already exists.'},
            status=status.HTTP_409_CONFLICT,
        )
    except (ImproperlyConfigured, SMTPException, OSError, ValueError):
        return Response(
            {'error': 'Verification email could not be sent. Please try again later.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({
        'message': 'Verification code sent.',
        'email': email,
        'requires_verification': True,
        'resend_available_in': int(OTP_RESEND_COOLDOWN.total_seconds()),
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([AllowAny])
def verify_otp(request):
    email = _validated_email(request.data.get('email'))
    otp = request.data.get('otp')
    if email is None:
        return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(otp, str) or re.fullmatch(r'[0-9]{6}', otp) is None:
        return Response({'error': 'Enter the 6-digit verification code.'}, status=status.HTTP_400_BAD_REQUEST)

    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return Response({'error': 'Invalid or expired verification code.'}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        otp_record = EmailOTP.objects.select_for_update().filter(user=user).first()
        if otp_record is None:
            return Response({'error': 'No active verification code. Request a new one.'}, status=status.HTTP_400_BAD_REQUEST)
        if otp_record.expires_at <= timezone.now():
            otp_record.delete()
            return Response({'error': 'Verification code expired. Request a new one.', 'expired': True}, status=status.HTTP_400_BAD_REQUEST)
        if otp_record.attempts >= OTP_MAX_ATTEMPTS:
            return Response({'error': 'Too many incorrect codes. Request a new one.', 'attempts_remaining': 0}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if not hmac.compare_digest(otp_record.otp_hash, _otp_digest(user, otp)):
            otp_record.attempts += 1
            otp_record.save(update_fields=['attempts'])
            remaining = OTP_MAX_ATTEMPTS - otp_record.attempts
            message = 'Invalid verification code.' if remaining else 'Too many incorrect codes. Request a new one.'
            response_status = status.HTTP_400_BAD_REQUEST if remaining else status.HTTP_429_TOO_MANY_REQUESTS
            return Response({'error': message, 'attempts_remaining': remaining}, status=response_status)

        user.is_active = True
        user.save(update_fields=['is_active'])
        otp_record.delete()

    login(request, user)
    return Response({
        'message': 'Email verified successfully.',
        'user': _user_to_dict(user),
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def resend_otp(request):
    email = _validated_email(request.data.get('email'))
    if email is None:
        return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return Response({'error': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)
    if user.is_active:
        return Response({'message': 'This email is already verified.', 'already_verified': True})

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=user.pk)
            if user.is_active:
                return Response({'message': 'This email is already verified.', 'already_verified': True})
            otp_record = EmailOTP.objects.select_for_update().filter(user=user).first()
            now = timezone.now()
            if otp_record and now < otp_record.resend_available_at:
                retry_after = max(1, int((otp_record.resend_available_at - now).total_seconds()))
                return Response(
                    {'error': 'Please wait before requesting another code.', 'retry_after': retry_after},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            otp = _new_otp()
            EmailOTP.objects.update_or_create(
                user=user,
                defaults={
                    'otp_hash': _otp_digest(user, otp),
                    'expires_at': now + OTP_TTL,
                    'attempts': 0,
                    'resend_available_at': now + OTP_RESEND_COOLDOWN,
                },
            )
            _send_otp(email, otp)
    except (ImproperlyConfigured, SMTPException, OSError, ValueError):
        return Response(
            {'error': 'Verification email could not be sent. Please try again later.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({'message': 'A new verification code was sent.', 'resend_available_in': 60})


@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """Authenticate with email + password."""
    email = request.data.get('email', '').strip().lower()
    password = request.data.get('password', '')

    if not email or not password:
        return Response(
            {'error': 'Email and password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Django authenticate uses username — we store email as username
    user = authenticate(request, username=email, password=password)

    if user is None:
        unverified_user = User.objects.filter(username=email, is_active=False).first()
        if unverified_user and unverified_user.check_password(password):
            return Response(
                {'error': 'Verify your email before signing in.', 'requires_verification': True, 'email': unverified_user.email},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {'error': 'Invalid email or password.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    login(request, user)

    return Response({
        'message': 'Login successful.',
        'user': _user_to_dict(user),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    """Clear the session."""
    logout(request)
    return Response({'message': 'Logged out successfully.'})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    """Return the currently authenticated user's info."""
    return Response({
        'user': _user_to_dict(request.user),
    })


def _user_to_dict(user):
    """Serialize a User object to a simple dict."""
    return {
        'id': user.id,
        'name': user.first_name or user.username,
        'email': user.email,
        'is_staff': user.is_staff,
        'date_joined': user.date_joined.isoformat(),
    }


@api_view(['GET'])
@permission_classes([AllowAny])
def csrf_token(request):
    return JsonResponse({'csrfToken': get_token(request)})
