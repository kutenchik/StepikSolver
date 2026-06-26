from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth.models import User
from .models import StepikProfile


class RegisterForm(UserCreationForm):
    """Форма регистрации с полями Stepik API."""
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'email@example.com',
            'autocomplete': 'email',
        }),
    )
    stepik_client_id = forms.CharField(
        max_length=255,
        required=False,
        label='Stepik Client ID',
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Ваш Client ID...',
            'autocomplete': 'off',
            'style': 'font-family: var(--font-mono);',
        }),
    )
    stepik_client_secret = forms.CharField(
        max_length=255,
        required=False,
        label='Stepik Client Secret',
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Ваш Client Secret...',
            'autocomplete': 'off',
            'style': 'font-family: var(--font-mono);',
        }),
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'password1', 'password2')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ('username', 'password1', 'password2'):
            self.fields[field_name].widget.attrs.update({
                'class': 'form-control',
            })
        self.fields['username'].widget.attrs['placeholder'] = 'Имя пользователя'
        self.fields['password1'].widget.attrs['placeholder'] = 'Пароль'
        self.fields['password2'].widget.attrs['placeholder'] = 'Повторите пароль'

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            StepikProfile.objects.update_or_create(
                user=user,
                defaults={
                    'stepik_client_id': self.cleaned_data.get('stepik_client_id', ''),
                    'stepik_client_secret': self.cleaned_data.get('stepik_client_secret', ''),
                },
            )
        return user


class LoginForm(AuthenticationForm):
    """Кастомная форма входа со стилями."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': 'Имя пользователя',
        })
        self.fields['password'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': 'Пароль',
        })


class StepikProfileForm(forms.ModelForm):
    """Форма редактирования Stepik API ключей."""

    class Meta:
        model = StepikProfile
        fields = ('stepik_client_id', 'stepik_client_secret')
        widgets = {
            'stepik_client_id': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Ваш Client ID...',
                'autocomplete': 'off',
                'style': 'font-family: var(--font-mono);',
            }),
            'stepik_client_secret': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Ваш Client Secret...',
                'autocomplete': 'off',
                'style': 'font-family: var(--font-mono);',
            }),
        }
