"""
REST API views for the Make Your Move assessment platform.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.views.decorators.csrf import ensure_csrf_cookie
from django.http import JsonResponse

@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({"detail": "CSRF cookie set"})
    path("csrf/", csrf),
    api.get('/csrf/');
    
from .models import (
    AssessmentSession, ActivityResponse, DomainScore,
    ThinkingStyle, ValidationResponse, DeepDiveChat,
)
from .serializers import (
    AssessmentSessionSerializer, ContextIntakeSerializer,
    ActivitySubmissionSerializer, ActivityResponseSerializer,
    DomainScoreSerializer, ThinkingStyleSerializer,
    ValidationSubmissionSerializer, ValidationResponseSerializer,
    DeepDiveChatSerializer, DeepDiveMessageSerializer,
)
from .scoring import (
    calculate_activity_scores, calculate_pre_assessment_scores,
    apply_validation_multipliers, get_final_results,
)
from .thinking_styles import save_thinking_styles, get_top_styles
from .signals_config import THINKING_STYLE_DOMAINS
from .deep_dive import (
    generate_opening_line, generate_follow_up,
    classify_reply, get_wrap_up_line, generate_roadmap_annotations,
)


def _get_or_create_session(request):
    """Get existing session for the authenticated user."""
    # First try session ID from Django session store
    session_id = request.session.get('assessment_session_id')
    if session_id:
        try:
            return AssessmentSession.objects.get(id=session_id)
        except AssessmentSession.DoesNotExist:
            pass

    # Fall back to looking up by authenticated user
    if request.user.is_authenticated:
        session = AssessmentSession.objects.filter(user=request.user).order_by('-created_at').first()
        if session:
            request.session['assessment_session_id'] = str(session.id)
            return session

    return None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def start_session(request):
    """Create a new assessment session linked to the authenticated user."""
    session = AssessmentSession.objects.create(
        user=request.user,
        session_key=request.session.session_key or '',
    )
    # Store in Django session
    request.session['assessment_session_id'] = str(session.id)
    request.session.save()

    return Response({
        'session_id': str(session.id),
        'current_stage': session.current_stage,
        'message': 'Assessment session started.',
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_session(request):
    """Get the current session status."""
    session = _get_or_create_session(request)
    if not session:
        return Response(
            {'error': 'No active session. Start a new one at /api/session/start/'},
            status=status.HTTP_404_NOT_FOUND,
        )
    serializer = AssessmentSessionSerializer(session)
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit_context(request):
    """Save context intake answers (3 questions)."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    serializer = ContextIntakeSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    session.year_of_study = serializer.validated_data['year_of_study']
    session.prior_experience = serializer.validated_data['prior_experience']
    session.goal = serializer.validated_data['goal']
    session.current_stage = 'pre_assessment'
    session.current_activity = 1
    session.save()

    # Determine activity order based on context
    activity_order = _get_activity_order(session)

    return Response({
        'message': 'Context intake saved.',
        'current_stage': session.current_stage,
        'activity_order': activity_order,
    })


def _get_activity_order(session):
    """
    Determine activity order based on context intake.
    Year 1-2 see System Flow first; Year 3-4+ see Architecture first.
    """
    default_order = [1, 2, 3, 4, 5, 6]

    if session.year_of_study in ('year_3', 'year_4_plus', 'professional'):
        # Architecture first for experienced students
        return [4, 1, 3, 2, 5, 6]

    return default_order


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit_activity(request, activity_number):
    """Submit response for a pre-assessment activity."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    if activity_number < 1 or activity_number > 6:
        return Response({'error': 'Invalid activity number.'}, status=status.HTTP_400_BAD_REQUEST)

    serializer = ActivitySubmissionSerializer(data={
        'activity_number': activity_number,
        'response_data': request.data.get('response_data', {}),
        'behavioral_signals': request.data.get('behavioral_signals', {}),
        'duration_ms': request.data.get('duration_ms', 0),
    })

    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    # Save activity response
    activity_response, created = ActivityResponse.objects.update_or_create(
        session=session,
        activity_number=activity_number,
        defaults={
            'response_data': serializer.validated_data['response_data'],
            'behavioral_signals': serializer.validated_data['behavioral_signals'],
            'duration_ms': serializer.validated_data['duration_ms'],
        }
    )

    # Calculate scores for this activity
    score_deltas, triggered_signals = calculate_activity_scores(
        activity_number,
        serializer.validated_data['response_data'],
    )

    # Update current activity tracker
    completed_count = ActivityResponse.objects.filter(session=session).count()
    if completed_count >= 6:
        session.current_stage = 'thinking_styles'
        session.current_activity = 6
    else:
        session.current_activity = activity_number
    session.save()

    return Response({
        'message': f'Activity {activity_number} response saved.',
        'signals_triggered': triggered_signals,
        'score_deltas': score_deltas,
        'activities_completed': completed_count,
        'next_stage': session.current_stage,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_thinking_styles(request):
    """Calculate and return thinking styles based on all activity responses."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    # Calculate pre-assessment scores
    domain_score, all_signals = calculate_pre_assessment_scores(session)

    # Calculate thinking styles
    styles = save_thinking_styles(session, domain_score.as_dict())

    # Enrich with config data
    for style in styles:
        config = THINKING_STYLE_DOMAINS.get(style['style_key'], {})
        style['icon'] = config.get('icon', '')
        style['primary_domains'] = config.get('primary_domains', [])

    session.current_stage = 'validation'
    session.save()

    return Response({
        'thinking_styles': styles,
        'domain_scores': DomainScoreSerializer(domain_score).data,
        'signals_log': all_signals,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit_validation(request, domain):
    """Submit a validation task response."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    serializer = ValidationSubmissionSerializer(data={
        'domain': domain,
        'task_type': request.data.get('task_type', ''),
        'response_data': request.data.get('response_data', {}),
        'engagement_level': request.data.get('engagement_level', 'partial'),
        'duration_ms': request.data.get('duration_ms', 0),
    })

    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    ValidationResponse.objects.create(
        session=session,
        **serializer.validated_data,
    )

    return Response({
        'message': f'Validation for {domain} saved.',
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_results(request):
    """Get final domain results with validation multipliers applied."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    # Ensure scores are calculated
    domain_score, _ = calculate_pre_assessment_scores(session)

    # Apply validation multipliers
    domain_score = apply_validation_multipliers(session)
    if not domain_score:
        return Response({'error': 'No scores available.'}, status=status.HTTP_404_NOT_FOUND)

    # Get thinking styles
    styles = ThinkingStyle.objects.filter(session=session)
    style_data = ThinkingStyleSerializer(styles, many=True).data

    # Enrich styles with config
    for style in style_data:
        config = THINKING_STYLE_DOMAINS.get(style['style_key'], {})
        style['icon'] = config.get('icon', '')
        style['primary_domains'] = config.get('primary_domains', [])

    session.current_stage = 'results'
    session.save()

    return Response({
        'domain_scores': DomainScoreSerializer(domain_score).data,
        'thinking_styles': style_data,
        'session': AssessmentSessionSerializer(session).data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_roadmap(request, domain):
    """Get personalized roadmap for a specific domain."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    # Roadmap data (placeholder — to be enhanced with AI later)
    roadmaps = {
        'backend': {
            'title': 'Backend Engineering',
            'description': 'Build the systems behind the interface.',
            'steps': [
                {'title': 'Python Fundamentals', 'level': 'beginner', 'duration': '2-3 weeks'},
                {'title': 'Django & REST APIs', 'level': 'beginner', 'duration': '3-4 weeks'},
                {'title': 'Database Design (SQL + PostgreSQL)', 'level': 'intermediate', 'duration': '2-3 weeks'},
                {'title': 'Authentication & Security', 'level': 'intermediate', 'duration': '2 weeks'},
                {'title': 'Caching & Performance (Redis)', 'level': 'intermediate', 'duration': '2 weeks'},
                {'title': 'System Design & Scalability', 'level': 'advanced', 'duration': '4-6 weeks'},
            ],
        },
        'frontend': {
            'title': 'Frontend Engineering',
            'description': 'Craft experiences people love to use.',
            'steps': [
                {'title': 'HTML, CSS & JavaScript Fundamentals', 'level': 'beginner', 'duration': '3-4 weeks'},
                {'title': 'React & Component Architecture', 'level': 'beginner', 'duration': '4-5 weeks'},
                {'title': 'State Management & API Integration', 'level': 'intermediate', 'duration': '2-3 weeks'},
                {'title': 'Responsive Design & Accessibility', 'level': 'intermediate', 'duration': '2 weeks'},
                {'title': 'Animation & Micro-interactions', 'level': 'intermediate', 'duration': '2 weeks'},
                {'title': 'Performance Optimization & Testing', 'level': 'advanced', 'duration': '3-4 weeks'},
            ],
        },
        'devops': {
            'title': 'DevOps Engineering',
            'description': 'Keep systems alive, fast, and reliable.',
            'steps': [
                {'title': 'Linux & Command Line', 'level': 'beginner', 'duration': '2-3 weeks'},
                {'title': 'Docker & Containerization', 'level': 'beginner', 'duration': '2-3 weeks'},
                {'title': 'CI/CD Pipelines', 'level': 'intermediate', 'duration': '3 weeks'},
                {'title': 'Cloud Platforms (AWS/GCP)', 'level': 'intermediate', 'duration': '4-5 weeks'},
                {'title': 'Monitoring & Observability', 'level': 'intermediate', 'duration': '2-3 weeks'},
                {'title': 'Infrastructure as Code (Terraform)', 'level': 'advanced', 'duration': '3-4 weeks'},
            ],
        },
        'data_eng': {
            'title': 'Data Engineering',
            'description': 'Build pipelines that turn raw data into insights.',
            'steps': [
                {'title': 'SQL & Data Modeling', 'level': 'beginner', 'duration': '2-3 weeks'},
                {'title': 'Python for Data (Pandas, NumPy)', 'level': 'beginner', 'duration': '3-4 weeks'},
                {'title': 'ETL Pipelines & Data Warehousing', 'level': 'intermediate', 'duration': '3-4 weeks'},
                {'title': 'Apache Spark Fundamentals', 'level': 'intermediate', 'duration': '3-4 weeks'},
                {'title': 'Streaming Data (Kafka)', 'level': 'advanced', 'duration': '3 weeks'},
                {'title': 'Data Quality & Governance', 'level': 'advanced', 'duration': '2-3 weeks'},
            ],
        },
        'ai_ml': {
            'title': 'AI/ML Engineering',
            'description': 'Teach machines to learn and decide.',
            'steps': [
                {'title': 'Python + Math Foundations', 'level': 'beginner', 'duration': '3-4 weeks'},
                {'title': 'Machine Learning Basics (scikit-learn)', 'level': 'beginner', 'duration': '4-5 weeks'},
                {'title': 'Deep Learning (PyTorch/TensorFlow)', 'level': 'intermediate', 'duration': '4-6 weeks'},
                {'title': 'NLP & Computer Vision', 'level': 'intermediate', 'duration': '4 weeks'},
                {'title': 'MLOps & Model Deployment', 'level': 'advanced', 'duration': '3-4 weeks'},
                {'title': 'LLMs & Generative AI', 'level': 'advanced', 'duration': '4-5 weeks'},
            ],
        },
        'cybersecurity': {
            'title': 'Cybersecurity',
            'description': 'Protect systems from those who would break them.',
            'steps': [
                {'title': 'Networking Fundamentals', 'level': 'beginner', 'duration': '2-3 weeks'},
                {'title': 'Web Security (OWASP Top 10)', 'level': 'beginner', 'duration': '3-4 weeks'},
                {'title': 'Cryptography Basics', 'level': 'intermediate', 'duration': '2-3 weeks'},
                {'title': 'Penetration Testing', 'level': 'intermediate', 'duration': '4-5 weeks'},
                {'title': 'Security Architecture & Threat Modeling', 'level': 'advanced', 'duration': '3-4 weeks'},
                {'title': 'Incident Response & Forensics', 'level': 'advanced', 'duration': '3-4 weeks'},
            ],
        },
        'product_eng': {
            'title': 'Product Engineering',
            'description': 'Bridge user needs and engineering decisions.',
            'steps': [
                {'title': 'Product Thinking Fundamentals', 'level': 'beginner', 'duration': '2-3 weeks'},
                {'title': 'User Research & UX Design', 'level': 'beginner', 'duration': '3-4 weeks'},
                {'title': 'Agile & Feature Prioritization', 'level': 'intermediate', 'duration': '2-3 weeks'},
                {'title': 'Technical Architecture for PMs', 'level': 'intermediate', 'duration': '3 weeks'},
                {'title': 'A/B Testing & Analytics', 'level': 'intermediate', 'duration': '2-3 weeks'},
                {'title': 'Stakeholder Communication', 'level': 'advanced', 'duration': '2-3 weeks'},
            ],
        },
    }

    roadmap = roadmaps.get(domain)
    if not roadmap:
        return Response({'error': f'Unknown domain: {domain}'}, status=status.HTTP_404_NOT_FOUND)

    # Include deep-dive annotations if chat was completed
    annotations = []
    if session:
        try:
            chat = DeepDiveChat.objects.get(session=session, domain=domain, status='completed')
            annotations = chat.roadmap_annotations or []
        except DeepDiveChat.DoesNotExist:
            pass

    roadmap['annotations'] = annotations
    roadmap['ai_notes'] = (
        annotations[0] if annotations
        else f'Personalized notes for {domain} will appear here when AI integration is enabled.'
    )

    return Response(roadmap)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reset_session(request):
    """Reset the current assessment session."""
    session = _get_or_create_session(request)
    if session:
        # Delete related data
        ActivityResponse.objects.filter(session=session).delete()
        DomainScore.objects.filter(session=session).delete()
        ThinkingStyle.objects.filter(session=session).delete()
        ValidationResponse.objects.filter(session=session).delete()
        DeepDiveChat.objects.filter(session=session).delete()
        session.delete()

    # Clear from Django session
    request.session.pop('assessment_session_id', None)

    return Response({'message': 'Session reset successfully.'})


# ═══════════════════════════════════════════════════════════════════════════════
# Deep-Dive Chat Views
# ═══════════════════════════════════════════════════════════════════════════════

MAX_AI_TURNS = 4
MAX_USER_TURNS = 4


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def start_deep_dive(request, domain):
    """Start (or resume) a conversational deep-dive for a domain."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    # Check if a chat already exists for this domain
    chat, created = DeepDiveChat.objects.get_or_create(
        session=session,
        domain=domain,
        defaults={'status': 'in_progress'},
    )

    # If already exists and has content, return current state (resume)
    if not created and chat.transcript:
        return Response({
            'chat': DeepDiveChatSerializer(chat).data,
            'message': 'Resuming existing deep-dive.',
        })

    # Generate opening line via Gemini
    opening = generate_opening_line(session, domain)

    chat.transcript = [{'role': 'ai', 'content': opening, 'turn': 0}]
    chat.ai_turn_count = 1
    chat.save()

    return Response({
        'chat': DeepDiveChatSerializer(chat).data,
        'message': 'Deep-dive started.',
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def send_deep_dive_message(request, domain):
    """Handle a user message in the deep-dive conversation."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    # Validate message
    serializer = DeepDiveMessageSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    user_content = serializer.validated_data['content']

    # Get existing chat
    try:
        chat = DeepDiveChat.objects.get(session=session, domain=domain)
    except DeepDiveChat.DoesNotExist:
        return Response(
            {'error': 'No deep-dive started for this domain. Call start first.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if chat.status != 'in_progress':
        return Response({
            'chat': DeepDiveChatSerializer(chat).data,
            'message': 'This conversation has already ended.',
        })

    # ── Guardrail: classify the reply ──
    classification = classify_reply(user_content)

    # Append user message to transcript
    user_turn = chat.user_turn_count + 1
    chat.transcript.append({
        'role': 'user',
        'content': user_content,
        'turn': user_turn,
    })
    chat.user_turn_count = user_turn

    # ── Handle hostile ──
    if classification == 'hostile':
        wrap_up = get_wrap_up_line('hostile')
        chat.transcript.append({'role': 'ai', 'content': wrap_up, 'turn': chat.ai_turn_count})
        chat.ai_turn_count += 1
        chat.status = 'terminated'
        chat.save()
        return Response({
            'chat': DeepDiveChatSerializer(chat).data,
            'message': 'Conversation terminated.',
        })

    # ── Handle personal situation ──
    if classification == 'personal':
        wrap_up = get_wrap_up_line('personal')
        chat.transcript.append({'role': 'ai', 'content': wrap_up, 'turn': chat.ai_turn_count})
        chat.ai_turn_count += 1
        chat.status = 'completed'
        chat.save()
        return Response({
            'chat': DeepDiveChatSerializer(chat).data,
            'message': 'Conversation completed.',
        })

    # ── Handle low-effort ──
    if classification == 'low_effort':
        # Count consecutive low-effort replies
        consecutive_low = 0
        for msg in reversed(chat.transcript):
            if msg['role'] == 'user':
                if classify_reply(msg['content']) == 'low_effort':
                    consecutive_low += 1
                else:
                    break
            else:
                break

        if consecutive_low >= 2:
            # Second low-effort → wrap up
            wrap_up = get_wrap_up_line('low_effort')
            chat.transcript.append({'role': 'ai', 'content': wrap_up, 'turn': chat.ai_turn_count})
            chat.ai_turn_count += 1
            chat.status = 'completed'
            chat.save()
            return Response({
                'chat': DeepDiveChatSerializer(chat).data,
                'message': 'Conversation completed (low effort).',
            })
        # else: first low-effort, Gemini will probe gently via system prompt

    # ── Check turn limits ──
    if chat.ai_turn_count >= MAX_AI_TURNS or chat.user_turn_count >= MAX_USER_TURNS:
        # Generate annotations and wrap up
        annotations = generate_roadmap_annotations(session, domain, chat.transcript)
        wrap_up = get_wrap_up_line('completed')
        chat.transcript.append({'role': 'ai', 'content': wrap_up, 'turn': chat.ai_turn_count})
        chat.ai_turn_count += 1
        chat.roadmap_annotations = annotations
        chat.status = 'completed'
        chat.save()
        return Response({
            'chat': DeepDiveChatSerializer(chat).data,
            'message': 'Conversation completed.',
        })

    # ── Generate follow-up via Gemini ──
    ai_turn_number = chat.ai_turn_count  # 1, 2, or 3 (0 was the opening)
    follow_up = generate_follow_up(session, domain, chat.transcript, ai_turn_number)

    chat.transcript.append({'role': 'ai', 'content': follow_up, 'turn': ai_turn_number})
    chat.ai_turn_count += 1

    # Check if this was the last AI turn
    if chat.ai_turn_count >= MAX_AI_TURNS:
        annotations = generate_roadmap_annotations(session, domain, chat.transcript)
        chat.roadmap_annotations = annotations
        chat.status = 'completed'

    chat.save()

    return Response({
        'chat': DeepDiveChatSerializer(chat).data,
        'message': 'Follow-up generated.' if chat.status == 'in_progress' else 'Conversation completed.',
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def skip_deep_dive(request, domain):
    """Skip the deep-dive for a domain."""
    session = _get_or_create_session(request)
    if not session:
        return Response({'error': 'No active session.'}, status=status.HTTP_404_NOT_FOUND)

    chat, created = DeepDiveChat.objects.get_or_create(
        session=session,
        domain=domain,
        defaults={'status': 'skipped'},
    )

    if not created:
        chat.status = 'skipped'
        chat.save()

    wrap_up = get_wrap_up_line('skipped')

    return Response({
        'chat': DeepDiveChatSerializer(chat).data,
        'wrap_up': wrap_up,
        'message': 'Deep-dive skipped.',
    })
