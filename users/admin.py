import re

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.auth.admin import UserAdmin
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html

from . import mapping
from .models import Process, UploadSettings, User
from django_celery_results.models import TaskResult
from .models import DailySummary

class CustomUserAdmin(UserAdmin):
    model = User

    # Ensure 'role' field appears in the user edit form with a dropdown
    fieldsets = UserAdmin.fieldsets + (
        ('Custom Fields', {'fields': ('role',)}),
    )

    # Ensure 'role' field appears in the user creation form with a dropdown
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Custom Fields', {'fields': ('role',)}),
    )

    list_display = ('username', 'email', 'role', 'is_staff', 'is_active')  # Display roles in the user list
    list_filter = ('role', 'is_staff', 'is_active')  # Add a filter in admin panel
    search_fields = ('username', 'email', 'role')

    # No save_model override: UserAdmin's forms already hash the password
    # (the add form via set_password, the change-password view via its own
    # form). Calling set_password again on obj.password re-hashed that hash,
    # so a user created here could not log in with the password they were
    # given until they reset it.

admin.site.register(User, CustomUserAdmin)

# @admin.register(TaskResult)
class TaskResultAdmin(admin.ModelAdmin):
    list_display = ('task_id', 'task_name', 'status', 'date_done', 'result')
    list_filter = ('status', 'date_done')
    search_fields = ('task_id', 'task_name')

@admin.register(DailySummary)
class DailySummaryAdmin(admin.ModelAdmin):
    list_display = ('date', 'process', 'pending', 'approved')
    list_filter = ('date', 'process')


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------
# Superusers can manage processes outright. Anyone else needs the "users |
# process" permissions granted on their account (Users > a user > User
# permissions): view to see the list, add/change to add, edit, remove from the
# dropdown and add back. Rows are never hard-deleted, so the delete permission
# is not used.

ATS_ID_PATTERN = re.compile(r'^ATS\d+$')


class ProcessAdminForm(forms.ModelForm):
    """Process and Location are picked from the HRMS mapping file."""

    name = forms.ChoiceField(label='Process Name')
    location = forms.ChoiceField()

    class Meta:
        model = Process
        fields = ('name', 'location', 'cluster_head_name', 'cluster_head_ats_id', 'is_active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        processes = mapping.get_mapping_processes()
        locations = mapping.get_locations()
        # Keep a saved value selectable even if HRMS has since dropped it, so an
        # old row can still be edited or taken off the upload page.
        if self.instance.pk:
            if self.instance.name not in processes:
                processes.append(self.instance.name)
            if self.instance.location not in locations:
                locations.append(self.instance.location)
        self.fields['name'].choices = [('', '--- Select Process ---')] + [
            (p, p) for p in sorted(processes, key=str.casefold)
        ]
        self.fields['location'].choices = [('', '--- Select Location ---')] + [
            (l, l) for l in sorted(locations, key=str.casefold)
        ]
        self.fields['name'].help_text = 'Processes in the HRMS mapping file.'
        self.fields['location'].help_text = (
            'Must be a location the HRMS mapping file lists for this process.'
        )
        self.fields['cluster_head_ats_id'].help_text = (
            "The L2 user with this ATS ID as username receives this process's "
            "files after L1, and its emails at that user's email address."
        )

    def clean_cluster_head_name(self):
        value = ' '.join(self.cleaned_data['cluster_head_name'].split())
        if not value:
            raise forms.ValidationError('Enter the Cluster Head name.')
        return value

    def clean_cluster_head_ats_id(self):
        value = mapping.normalise_ats_id(self.cleaned_data['cluster_head_ats_id'])
        if not ATS_ID_PATTERN.match(value):
            raise forms.ValidationError('Enter an ATS ID such as ATS12345.')
        return value

    def clean(self):
        cleaned = super().clean()
        name, location = cleaned.get('name'), cleaned.get('location')
        if not (name and location):
            return cleaned

        # Unchanged values on an existing row are not re-checked, so a row HRMS
        # has since dropped can still be edited or taken off the upload page.
        unchanged = bool(
            self.instance.pk
            and name == self.instance.name and location == self.instance.location
        )
        valid_locations = mapping.locations_for_process(name)
        if not unchanged and location not in valid_locations:
            self.add_error(
                'location',
                "'%s' is not a location of '%s' in the HRMS mapping file. "
                "Choose one of: %s." % (
                    location, name, ', '.join(valid_locations) or 'none listed'
                ),
            )

        duplicate = Process.objects.filter(
            name__iexact=name, location__iexact=location
        ).exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError(
                "'%s' at '%s' already exists. Edit that entry instead." % (name, location)
            )
        return cleaned


@admin.register(Process)
class ProcessAdmin(admin.ModelAdmin):
    form = ProcessAdminForm
    list_display = (
        'name', 'location', 'cluster_head_name', 'cluster_head_ats_id',
        'on_upload_page', 'edit_link',
    )
    list_display_links = ('name',)
    list_filter = ('is_active', 'location')
    search_fields = ('name', 'location', 'cluster_head_name', 'cluster_head_ats_id')
    actions = ['remove_from_upload_page', 'add_back_to_upload_page']
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('name', 'location')}),
        ('Cluster Head (L2)', {'fields': ('cluster_head_name', 'cluster_head_ats_id')}),
        ('Upload page', {'fields': ('is_active',)}),
        ('History', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    @admin.display(boolean=True, description='On upload page', ordering='is_active')
    def on_upload_page(self, obj):
        return obj.is_active

    @admin.display(description='Actions')
    def edit_link(self, obj):
        url = reverse('admin:users_process_change', args=[obj.pk])
        return format_html('<a class="button" href="{}">Edit</a>', url)

    # Rows are kept so pending files keep routing and history stays readable;
    # "remove" takes a process off the upload dropdown instead.
    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(
        description='Remove selected from the upload Process dropdown',
        permissions=['change'],
    )
    def remove_from_upload_page(self, request, queryset):
        if request.POST.get('post') != 'yes':
            return TemplateResponse(
                request, 'admin/users/process/remove_confirmation.html', {
                    **self.admin_site.each_context(request),
                    'title': 'Remove from the upload page?',
                    'opts': self.model._meta,
                    'processes': queryset,
                    'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
                },
            )
        count = queryset.filter(is_active=True).update(is_active=False)
        self.message_user(
            request,
            '%s process(es) removed from the upload dropdown. Uploaded files, their '
            'history and files still pending are not affected.' % count,
            messages.SUCCESS,
        )

    @admin.action(
        description='Add selected back to the upload Process dropdown',
        permissions=['change'],
    )
    def add_back_to_upload_page(self, request, queryset):
        count = queryset.filter(is_active=False).update(is_active=True)
        self.message_user(
            request, '%s process(es) added back to the upload dropdown.' % count,
            messages.SUCCESS,
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        head = User.objects.filter(
            username__iexact=obj.cluster_head_ats_id, is_active=True
        ).first()
        if head is None or head.role != 'l2':
            self.message_user(
                request,
                'No active L2 user has the username %s. Until one is created under '
                'Users with role "L2 User", %s files are seen only by L2 users who '
                'are not Cluster Heads.' % (obj.cluster_head_ats_id, obj),
                messages.WARNING,
            )
        elif not head.email:
            self.message_user(
                request,
                'L2 user %s has no email address, so they will not receive the '
                'upload or daily reminder emails. Add one under Users.'
                % obj.cluster_head_ats_id,
                messages.WARNING,
            )


# ---------------------------------------------------------------------------
# Upload Settings
# ---------------------------------------------------------------------------
# One row, edited in place. Same permission model as Processes: superusers, or
# staff granted the "users | upload settings" view/change permissions.

@admin.register(UploadSettings)
class UploadSettingsAdmin(admin.ModelAdmin):
    fields = ('allowed_upload_days', 'updated_at', 'updated_by')
    readonly_fields = ('updated_at', 'updated_by')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # There is only ever the one row, so open it straight away.
        UploadSettings.objects.get_or_create(pk=1)
        return redirect('admin:users_uploadsettings_change', 1)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        self.message_user(
            request,
            'Uploads now accept dates from today back to %s day(s) before today. '
            'This applies to the next upload; no restart is needed.'
            % (obj.allowed_upload_days - 1),
            messages.SUCCESS,
        )

    def response_change(self, request, obj):
        # The changelist only redirects back here, so stay on the form.
        return redirect('admin:users_uploadsettings_change', obj.pk)


# Django lists an app's models alphabetically, which would put Processes above
# Users. Keep Users first, Processes directly below it and Upload Settings
# after that; the rest keep their normal order.
_SIDEBAR_ORDER = {'User': 0, 'Process': 1, 'UploadSettings': 2}
_default_get_app_list = admin.site.get_app_list


def _get_app_list(request, app_label=None):
    app_list = _default_get_app_list(request, app_label)
    for app in app_list:
        if app['app_label'] == 'users':
            app['models'].sort(
                key=lambda m: (_SIDEBAR_ORDER.get(m['object_name'], 3), m['name'])
            )
    return app_list


admin.site.get_app_list = _get_app_list
