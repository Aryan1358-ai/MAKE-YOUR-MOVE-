"""URL routing for the assessment API."""

from django.urls import path
from . import views
from . import auth_views
from . import analytics_views

app_name = 'assessment'

urlpatterns = [
    # Authentication
    path('csrf/', auth_views.csrf_token, name='csrf-token'),
    path('auth/register/', auth_views.register, name='register'),
    path('auth/login/', auth_views.login_view, name='login'),
    path('auth/logout/', auth_views.logout_view, name='logout'),
    path('auth/me/', auth_views.me, name='me'),

    # Session management
    path('session/start/', views.start_session, name='start-session'),
    path('session/', views.get_session, name='get-session'),
    path('session/reset/', views.reset_session, name='reset-session'),

    # Context intake
    path('session/context/', views.submit_context, name='submit-context'),

    # Pre-assessment activities
    path('activity/<int:activity_number>/submit/', views.submit_activity, name='submit-activity'),

    # Thinking styles
    path('thinking-styles/', views.get_thinking_styles, name='thinking-styles'),

    # Validation tasks
    path('validation/<str:domain>/submit/', views.submit_validation, name='submit-validation'),

    # Results
    path('results/', views.get_results, name='results'),

    # Roadmap
    path('roadmap/<str:domain>/', views.get_roadmap, name='roadmap'),

    # Deep-Dive Chat
    path('deep-dive/<str:domain>/start/', views.start_deep_dive, name='start-deep-dive'),
    path('deep-dive/<str:domain>/message/', views.send_deep_dive_message, name='deep-dive-message'),
    path('deep-dive/<str:domain>/skip/', views.skip_deep_dive, name='skip-deep-dive'),

    # Analytics (admin only)
    path('analytics/', analytics_views.get_analytics, name='analytics'),
]

