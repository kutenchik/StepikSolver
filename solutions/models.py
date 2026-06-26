from django.db import models
from django.contrib.auth.models import User

class StepikProfile(models.Model):
    """Хранение Stepik API ключей для каждого пользователя."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='stepik_profile')
    stepik_client_id = models.CharField(max_length=255, blank=True, default='', verbose_name='Stepik Client ID')
    stepik_client_secret = models.CharField(max_length=255, blank=True, default='', verbose_name='Stepik Client Secret')

    def __str__(self):
        return f"StepikProfile: {self.user.username}"

    def has_credentials(self):
        return bool(self.stepik_client_id and self.stepik_client_secret)


class Course(models.Model):
    stepik_id = models.PositiveIntegerField(unique=True, verbose_name="ID на Stepik")
    title = models.CharField(max_length=255, verbose_name="Название курса")
    cover_url = models.URLField(blank=True, null=True, verbose_name="Ссылка на обложку")
    description = models.TextField(blank=True, verbose_name="Описание")
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="Кто добавил")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title

class CourseRun(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Ожидание'),
        ('running', 'Выполняется'),
        ('completed', 'Завершён'),
        ('failed', 'Ошибка'),
        ('stopped', 'Остановлен'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="Пользователь")
    course = models.ForeignKey(Course, on_delete=models.CASCADE, verbose_name="Курс")
    target_percent = models.PositiveIntegerField(verbose_name="Целевой %")
    target_score = models.IntegerField(default=0, verbose_name="Целевой балл")
    current_score = models.IntegerField(default=0, verbose_name="Текущий балл")
    total_score = models.IntegerField(default=0, verbose_name="Всего баллов")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="Статус")
    log = models.TextField(blank=True, default='', verbose_name="Лог")
    steps_total = models.IntegerField(default=0, verbose_name="Всего шагов")
    steps_done = models.IntegerField(default=0, verbose_name="Пройдено шагов")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")

    def __str__(self):
        return f"Run {self.id}: {self.course.title} ({self.status})"

class Section(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='sections')
    stepik_id = models.PositiveIntegerField(unique=True)
    title = models.CharField(max_length=255)
    position = models.PositiveIntegerField(default=0) # Порядок в курсе

    class Meta:
        ordering = ['position']

class Lesson(models.Model):
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name='lessons')
    stepik_id = models.PositiveIntegerField(unique=True)
    title = models.CharField(max_length=255)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['position']

class Task(models.Model):
    TASK_TYPES = [
        ('choice', 'Выбор варианта'),
        ('string', 'Строковый ответ'),
        ('code', 'Программирование'),
        ('number', 'Числовой ответ'),
        ('math', 'Математика'),
        ('sorting', 'Сортировка'),
        ('matching', 'Сопоставление'),
        ('fill-blanks', 'Заполнение пропусков'),
        ('free-answer', 'Свободный ответ'),
        ('table', 'Таблица'),
        ('text', 'Текст/Видео'),
        ('other', 'Другое'),
    ]

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='tasks')
    stepik_id = models.PositiveIntegerField(unique=True)
    task_type = models.CharField(max_length=30, choices=TASK_TYPES, default='other')
    question_text = models.TextField(verbose_name="Текст задания", blank=True, default='')
    correct_answer = models.TextField(verbose_name="Правильный ответ", blank=True, default='')
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['position']

    def __str__(self):
        return f"Шаг {self.stepik_id} ({self.get_task_type_display()})"