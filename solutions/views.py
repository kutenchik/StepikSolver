import requests
import json
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from .models import Course, Section, Lesson, Task, StepikProfile, CourseRun
from .forms import RegisterForm, LoginForm, StepikProfileForm


def get_stepik_token(user=None):
    """
    Получить OAuth2 access token для Stepik API (client_credentials).
    Использует персональные ключи пользователя, если они есть,
    иначе — глобальные из settings.
    """
    client_id = settings.STEPIK_CLIENT_ID
    client_secret = settings.STEPIK_CLIENT_SECRET

    if user and user.is_authenticated:
        try:
            profile = user.stepik_profile
            if profile.has_credentials():
                client_id = profile.stepik_client_id
                client_secret = profile.stepik_client_secret
        except StepikProfile.DoesNotExist:
            pass

    if not client_id or not client_secret:
        raise ValueError("Stepik API ключи не настроены. Укажите их в настройках профиля.")

    response = requests.post(
        'https://stepik.org/oauth2/token/',
        data={'grant_type': 'client_credentials'},
        auth=(client_id, client_secret),
    )
    response.raise_for_status()
    return response.json().get('access_token')


def stepik_api_get(url, params=None, token=None):
    """GET-запрос к Stepik API с авторизацией."""
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    r = requests.get(url, params=params, headers=headers)
    r.raise_for_status()
    return r.json()


# Типы заданий, для которых НЕ подтягиваем ответ автоматически
SKIP_ANSWER_TYPES = ('free-answer',)


def fetch_correct_answer(step_id, block_name, token):
    """
    Получить правильный ответ из submissions API.
    Возвращает строку с читаемым ответом или '' если не найден.
    """
    api_url = "https://stepik.org/api"

    if block_name in SKIP_ANSWER_TYPES:
        return ''

    try:
        # 1. Получаем правильный submission
        sub_data = stepik_api_get(
            f"{api_url}/submissions",
            params={'step': step_id, 'status': 'correct', 'page_size': 1},
            token=token
        )
        submissions = sub_data.get('submissions', [])
        if not submissions:
            return ''

        sub = submissions[0]
        reply = sub.get('reply', {})
        attempt_id = sub.get('attempt')

        # 2. Получаем attempt для dataset (варианты ответов)
        attempt_data = stepik_api_get(f"{api_url}/attempts/{attempt_id}", token=token)
        attempts = attempt_data.get('attempts', [])
        dataset = attempts[0].get('dataset', {}) if attempts else {}

        # 3. Формируем читаемый ответ в зависимости от типа
        return format_answer(block_name, reply, dataset)

    except Exception:
        return ''


def format_answer(block_name, reply, dataset):
    """Форматирует ответ в читаемый вид на основе типа задания."""
    if block_name == 'choice':
        choices = reply.get('choices', [])
        options = dataset.get('options', [])
        correct = [opt for opt, chosen in zip(options, choices) if chosen]
        # Сохраняем JSON-массивом — это единственный надёжный формат,
        # т.к. сами тексты опций могут содержать запятые
        return json.dumps(correct, ensure_ascii=False)

    elif block_name == 'matching':
        # reply.ordering = [0, 2, 1] + dataset.pairs = [{first, second}, ...]
        ordering = reply.get('ordering', [])
        pairs = dataset.get('pairs', [])
        if pairs and ordering:
            first_col = [p['first'] for p in pairs]
            second_col = [p['second'] for p in pairs]
            lines = []
            for i, idx in enumerate(ordering):
                if i < len(first_col) and idx < len(second_col):
                    lines.append(f"{first_col[i]} -> {second_col[idx]}")
            return '\n'.join(lines)
        return json.dumps(reply, ensure_ascii=False)

    elif block_name == 'sorting':
        # reply.ordering = [2, 0, 1] + dataset.options = ['A', 'B', 'C']
        ordering = reply.get('ordering', [])
        options = dataset.get('options', [])
        if options and ordering:
            sorted_items = [options[i] for i in ordering if i < len(options)]
            return '\n'.join(f"{idx+1}. {item}" for idx, item in enumerate(sorted_items))
        return json.dumps(reply, ensure_ascii=False)

    elif block_name == 'string':
        return reply.get('text', '') or reply.get('files', '')

    elif block_name == 'number':
        return reply.get('number', '')

    elif block_name == 'math':
        return reply.get('formula', '')

    elif block_name == 'code':
        code = reply.get('code', '')
        language = reply.get('language', '')
        if language:
            return f"[{language}]\n{code}"
        return code

    elif block_name == 'fill-blanks':
        blanks = reply.get('blanks', [])
        return ', '.join(blanks) if blanks else json.dumps(reply, ensure_ascii=False)

    elif block_name == 'table':
        # reply.choices = [[true, false], [false, true], ...] + dataset
        choices = reply.get('choices', [])
        rows = dataset.get('rows', [])
        columns = dataset.get('columns', [])
        if rows and columns and choices:
            lines = []
            for i, row in enumerate(rows):
                if i < len(choices):
                    selected = [col for col, val in zip(columns, choices[i]) if val]
                    lines.append(f"{row}: {', '.join(selected)}")
            return '\n'.join(lines)
        return json.dumps(reply, ensure_ascii=False)

    else:
        # Для неизвестных типов сохраняем JSON
        return json.dumps(reply, ensure_ascii=False)


# ===== AUTH VIEWS =====

def register_view(request):
    """Регистрация нового пользователя."""
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, f"Добро пожаловать, {user.username}! Аккаунт создан.")
            return redirect('home')
    else:
        form = RegisterForm()
    return render(request, 'solutions/register.html', {'form': form})


def login_view(request):
    """Вход в систему."""
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f"С возвращением, {user.username}!")
            next_url = request.GET.get('next', 'home')
            return redirect(next_url)
    else:
        form = LoginForm()
    return render(request, 'solutions/login.html', {'form': form})


def logout_view(request):
    """Выход из системы."""
    logout(request)
    messages.info(request, "Вы вышли из системы.")
    return redirect('home')


@login_required
def profile_view(request):
    """Настройки профиля: Stepik API ключи."""
    profile, _ = StepikProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        form = StepikProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Stepik API ключи обновлены!")
            return redirect('profile')
    else:
        form = StepikProfileForm(instance=profile)
    return render(request, 'solutions/profile.html', {'form': form, 'profile': profile})


# ===== MAIN VIEWS =====

# 1. Главная страница со списком курсов
def home(request):
    courses = Course.objects.all()
    return render(request, 'solutions/course_list.html', {'courses': courses})

# 2. Страница конкретного курса
def course_detail(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    return render(request, 'solutions/course_detail.html', {'course': course})

# 3. Логика добавления курса (Парсер с OAuth2 + подтягивание ответов)
@login_required
def add_course(request):
    if request.method == 'POST':
        course_id = request.POST.get('stepik_id')
        api_url = "https://stepik.org/api"
        
        # Получаем токен авторизации (используя ключи пользователя)
        try:
            token = get_stepik_token(user=request.user)
        except Exception as e:
            messages.error(request, f"Ошибка авторизации Stepik API: {e}")
            return redirect('add_course')
        
        # 1. Курс
        try:
            course_data = stepik_api_get(f"{api_url}/courses/{course_id}", token=token)['courses'][0]
        except Exception:
            messages.error(request, "Курс не найден на Stepik!")
            return redirect('add_course')
        
        course, _ = Course.objects.get_or_create(
            stepik_id=course_id,
            defaults={
                'title': course_data['title'],
                'cover_url': course_data.get('cover', ''),
                'description': course_data.get('summary', ''),
                'added_by': request.user,
            }
        )

        answers_count = 0

        # 2. Секции
        section_ids = course_data.get('sections', [])
        if section_ids:
            sections_data = stepik_api_get(f"{api_url}/sections", params={'ids[]': section_ids}, token=token)
            
            for sec_data in sections_data.get('sections', []):
                section, _ = Section.objects.get_or_create(
                    course=course,
                    stepik_id=sec_data['id'],
                    defaults={'title': sec_data['title'], 'position': sec_data['position']}
                )
                
                # 3. Юниты -> Уроки
                unit_ids = sec_data.get('units', [])
                if unit_ids:
                    units_data = stepik_api_get(f"{api_url}/units", params={'ids[]': unit_ids}, token=token)
                    lesson_ids = [u['lesson'] for u in units_data.get('units', [])]
                    unit_positions = {u['lesson']: u['position'] for u in units_data.get('units', [])}
                    
                    if lesson_ids:
                        lessons_data = stepik_api_get(f"{api_url}/lessons", params={'ids[]': lesson_ids}, token=token)
                        
                        for l_data in lessons_data.get('lessons', []):
                            lesson, _ = Lesson.objects.get_or_create(
                                section=section,
                                stepik_id=l_data['id'],
                                defaults={
                                    'title': l_data['title'],
                                    'position': unit_positions.get(l_data['id'], 0)
                                }
                            )

                            # 4. Шаги (задания)
                            step_ids = l_data.get('steps', [])
                            if step_ids:
                                steps_data = stepik_api_get(f"{api_url}/steps", params={'ids[]': step_ids}, token=token)
                                
                                for s_data in steps_data.get('steps', []):
                                    block = s_data.get('block', {})
                                    block_name = block.get('name', 'other')
                                    question_text = block.get('text', '')
                                    
                                    # Пропускаем видео и текстовые шаги без вопросов
                                    if block_name in ('video', 'text'):
                                        continue
                                    
                                    # 5. Подтягиваем правильный ответ из submissions
                                    correct_answer = fetch_correct_answer(s_data['id'], block_name, token)
                                    if correct_answer:
                                        answers_count += 1
                                    
                                    Task.objects.get_or_create(
                                        lesson=lesson,
                                        stepik_id=s_data['id'],
                                        defaults={
                                            'task_type': block_name if block_name in dict(Task.TASK_TYPES) else 'other',
                                            'question_text': question_text,
                                            'correct_answer': correct_answer,
                                            'position': s_data.get('position', 0),
                                        }
                                    )

        messages.success(request, f"Курс '{course.title}' успешно импортирован! Ответов подтянуто: {answers_count}")
        return redirect('course_detail', course_id=course.id)
    return render(request, 'solutions/add_course.html')


# 4. Сохранение ответа пользователем
@login_required
def save_answer(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if request.method == 'POST':
        answer_text = request.POST.get('answer', '').strip()
        if answer_text:
            task.correct_answer = answer_text
            task.save()
            messages.success(request, "Ответ сохранён!")
        else:
            messages.warning(request, "Ответ не может быть пустым!")
    return redirect('course_detail', course_id=task.lesson.section.course.id)

# 5. Автопрохождение курса
import threading
from django.http import JsonResponse
from openai import OpenAI
from .stepik_solver import StepikSolver

@login_required
def run_course(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    
    # Пытаемся узнать текущий балл (нужен токен)
    try:
        token = get_stepik_token(user=request.user)
    except Exception as e:
        messages.error(request, f"Ошибка авторизации: {e}")
        return redirect('course_detail', course_id=course.id)
    
    # Временный объект solver только для того чтобы узнать score
    temp_run = CourseRun(user=request.user, course=course, target_percent=100)
    solver = StepikSolver(token=token, course_run=temp_run, ai_client=None)
    current_score, total_score = solver.get_course_score(course.stepik_id)
    
    if request.method == 'POST':
        target_percent = int(request.POST.get('target_percent', 100))
        
        run = CourseRun.objects.create(
            user=request.user,
            course=course,
            target_percent=target_percent,
            current_score=current_score,
            total_score=total_score
        )
        
        # Запускаем в фоне
        client = OpenAI(api_key=settings.ALEM_API_KEY, base_url=settings.ALEM_API_URL)
        solver_instance = StepikSolver(token=token, course_run=run, ai_client=client)
        
        thread = threading.Thread(target=solver_instance.run)
        thread.daemon = True
        thread.start()
        
        return redirect('run_status', run_id=run.id)
        
    return render(request, 'solutions/run_course.html', {
        'course': course,
        'current_score': current_score,
        'total_score': total_score
    })

@login_required
def run_status(request, run_id):
    run = get_object_or_404(CourseRun, id=run_id, user=request.user)
    return render(request, 'solutions/run_status.html', {'run': run})

@login_required
def run_status_api(request, run_id):
    run = get_object_or_404(CourseRun, id=run_id, user=request.user)
    return JsonResponse({
        'status': run.status,
        'status_display': run.get_status_display(),
        'steps_done': run.steps_done,
        'steps_total': run.steps_total,
        'current_score': run.current_score,
        'target_score': run.target_score,
        'total_score': run.total_score,
        'log': run.log
    })

@login_required
def stop_run(request, run_id):
    run = get_object_or_404(CourseRun, id=run_id, user=request.user)
    if run.status in ['pending', 'running']:
        run.status = 'stopped'
        run.log += f"\n[INFO] Пользователь запросил остановку прохождения.\n"
        run.save()
        messages.info(request, "Процесс прохождения остановлен.")
    return redirect('run_status', run_id=run.id)