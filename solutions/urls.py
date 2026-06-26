from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('add/', views.add_course, name='add_course'),
    path('course/<int:course_id>/', views.course_detail, name='course_detail'),
    path('task/<int:task_id>/answer/', views.save_answer, name='save_answer'),
    
    # Run Course
    path('course/<int:course_id>/run/', views.run_course, name='run_course'),
    path('run/<int:run_id>/status/', views.run_status, name='run_status'),
    path('run/<int:run_id>/api/', views.run_status_api, name='run_status_api'),
    path('run/<int:run_id>/stop/', views.stop_run, name='stop_run'),

    # Auth
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
]