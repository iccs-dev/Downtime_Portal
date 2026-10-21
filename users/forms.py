from django import forms
# from django.contrib.auth.models import User
import os
from django.contrib.auth import get_user_model

from .mapping import allowed_locations_for_process, get_locations, get_processes

User = get_user_model()
class CSVUploadForm(forms.Form):
    # Choices are filled in __init__ from media/mapping/mapping.csv, never
    # here, so the workbook stays the only place a process or location is
    # defined.
    process = forms.ChoiceField(
        label='Process',
        choices=[],
        required=True,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    location = forms.ChoiceField(
        label='Location',
        choices=[],
        required=True,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    file = forms.FileField(
        label='Upload File',
        widget=forms.FileInput(attrs={'accept': '.csv, .xls, .xlsx'})
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        # Unique, blank-free values straight from the mapping file. The empty
        # entry forces a deliberate choice instead of silently defaulting to
        # whichever process happens to sort first.
        self.fields['process'].choices = (
            [('', '--- Select Process ---')]
            + [(p, p) for p in get_processes()]
        )
        self.fields['location'].choices = (
            [('', '--- Select Location ---')]
            + [(l, l) for l in get_locations()]
        )

    def clean_file(self):
        file = self.cleaned_data.get('file')

        # Ensure a file is uploaded
        if not file:
            raise forms.ValidationError("No file uploaded.")

        # Check file extension
        allowed_extensions = ['.csv', '.xls', '.xlsx']
        ext = os.path.splitext(file.name)[1].lower()

        if ext not in allowed_extensions:
            raise forms.ValidationError("Only .csv, .xls, and .xlsx files are allowed.")

        return file

    def clean(self):
        cleaned_data = super().clean()
        if self.user and self.user.role != 'regular':
            raise forms.ValidationError("You do not have permission to upload files.")

        # The Location dropdown only offers the sites the chosen Process runs
        # at, but that is a browser-side restriction. Re-check it here so a
        # posted pair that was never offered is rejected on the server too.
        process = cleaned_data.get('process')
        location = cleaned_data.get('location')
        # Only the locations both the mapping file and an active Process row
        # allow; a process taken off a site in the admin is refused there too.
        if process and location:
            allowed = allowed_locations_for_process(process)
            if location not in allowed:
                self.add_error(
                    'location',
                    "'%s' is not a valid Location for the Process '%s'. "
                    "Allowed: %s." % (location, process, ', '.join(allowed)),
                )

        return cleaned_data



class UserCreationForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput)
    
    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'is_staff', 'is_superuser']
    
    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password'])
        if commit:
            user.save()
        return user