"""
Authentication API views using Django's built-in User model.

Endpoints:
  POST /api/auth/register/  — create account + auto-login
  POST /api/auth/login/     — email + password → session cookie
  POST /api/auth/logout/    — clear session
  GET  /api/auth/me/        — return current user info (or 401)
"""
from django.contrib.auth import authenticate, login, logout
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth.models import User
from django.db import IntegrityError

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from django.middleware.csrf import get_token
from django.http import JsonResponse


@api_view(['GET'])
@permission_classes([AllowAny])
def csrf_token(request):
    token = get_token(request)
    return JsonResponse({
        'csrfToken': token
    })
  
@api_view(['POST'])
@permission_classes([AllowAny])
def register(request):
    """Create a new user account and auto-login."""
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

    # Use email as the username (Django requires unique username)
    try:
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=name,
        )
    except IntegrityError:
        return Response(
            {'error': 'An account with this email already exists.'},
            status=status.HTTP_409_CONFLICT,
        )

    # Auto-login after registration
    login(request, user)

    return Response({
        'message': 'Account created successfully.',
        'user': _user_to_dict(user),
    }, status=status.HTTP_201_CREATED)


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
