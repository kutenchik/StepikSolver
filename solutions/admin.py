from django.contrib import admin
from .models import Course, Section, Lesson, Task, StepikProfile, CourseRun

@admin.register(CourseRun)
class CourseRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'course', 'user', 'status', 'target_percent', 'current_score', 'created_at')
    list_filter = ('status',)
    search_fields = ('course__title', 'user__username')

admin.site.register(Course)
admin.site.register(Section)
admin.site.register(Lesson)
admin.site.register(Task)
admin.site.register(StepikProfile)