import json

from django.views.generic import FormView, View
from django.views.generic.detail import SingleObjectMixin
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext_lazy as _

from .forms import InviteForm, CleanEmailMixin
from .exceptions import AlreadyInvited, AlreadyAccepted, UserRegisteredEmail
from .app_settings import app_settings
from .adapters import get_invitations_adapter
from .signals import invite_accepted
from .utils import get_invitation_model

Invitation = get_invitation_model()


class SendInvite(FormView):
    template_name = 'invitations/forms/_invite.html'
    form_class = InviteForm

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        return super(SendInvite, self).dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        email = form.cleaned_data["email"]

        try:
            invite = form.save(email)
            invite.inviter = self.request.user
            invite.save()
            invite.send_invitation(self.request)
        except Exception:
            return self.form_invalid(form)
        return self.render_to_response(
            self.get_context_data(
                success_message=_('%(email)s has been invited') % {
                    "email": email}))

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))


class SendJSONInvite(View):
    http_method_names = [u'post']

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        if app_settings.ALLOW_JSON_INVITES:
            return super(SendJSONInvite, self).dispatch(
                request, *args, **kwargs)
        else:
            raise Http404

    def post(self, request, *args, **kwargs):
        status_code = 400
        invitees = json.loads(request.body.decode())
        response = {'valid': [], 'invalid': []}
        if isinstance(invitees, list):
            for invitee in invitees:
                try:
                    validate_email(invitee)
                    CleanEmailMixin().validate_invitation(invitee)
                    invite = Invitation.create(invitee)
                except(ValueError, KeyError):
                    pass
                except(ValidationError):
                    response['invalid'].append({
                        invitee: 'invalid email'})
                except(AlreadyAccepted):
                    response['invalid'].append({
                        invitee: 'already accepted'})
                except(AlreadyInvited):
                    response['invalid'].append(
                        {invitee: 'pending invite'})
                except(UserRegisteredEmail):
                    response['invalid'].append(
                        {invitee: 'user registered email'})
                else:
                    invite.send_invitation(request)
                    response['valid'].append({invitee: 'invited'})

        if response['valid']:
            status_code = 201

        return HttpResponse(
            json.dumps(response),
            status=status_code, content_type='application/json')


class AcceptInvite(SingleObjectMixin, View):
    form_class = InviteForm

    def get_signup_redirect(self):
        return app_settings.SIGNUP_REDIRECT

    def get(self, *args, **kwargs):
        if app_settings.CONFIRM_INVITE_ON_GET:
            return self.post(*args, **kwargs)
        else:
            if self.request.is_ajax:
                invitation = self.get_object()
                if not invitation or invitation.accepted or invitation.key_expired():
                    raise Http404()
                response = {
                    'invitation': {
                        'email': invitation.email,
                        'key': invitation.key,
                        'sent': invitation.sent.isoformat() if invitation.sent else None,
                        'inviter': {
                            'username': invitation.inviter.username,
                            'email': invitation.inviter.email,
                            'full_name': invitation.inviter.get_full_name(),
                            'first_name': invitation.inviter.first_name,
                            'last_name': invitation.inviter.last_name,
                        } if invitation.inviter else None
                    } if invitation else None
                }
                return JsonResponse(response)
            else:
                raise Http404()

    def post(self, *args, **kwargs):
        self.object = invitation = self.get_object()

        # Compatibility with older versions: return an HTTP 410 GONE if there
        # is an error. # Error conditions are: no key, expired key or
        # previously accepted key.
        if app_settings.GONE_ON_ACCEPT_ERROR and \
                (not invitation or
                 (invitation and (invitation.accepted or
                                  invitation.key_expired()))):
            return HttpResponse(status=410)

        # No invitation was found.
        if not invitation:
            # Newer behavior: show an error message and redirect.
            if self.request.is_ajax:
                return JsonResponse({
                    'ok': False,
                    'error': 'INVITATION_INVALID'
                }, status=410)
            else:
                get_invitations_adapter().add_message(
                    self.request,
                    messages.ERROR,
                    'invitations/messages/invite_invalid.txt')
                return redirect(app_settings.LOGIN_REDIRECT)

        # The invitation was previously accepted, redirect to the login
        # view.
        if invitation.accepted:
            if self.request.is_ajax:
                return JsonResponse({
                    'ok': False,
                    'error': 'INVITATION_ALREADY_ACCEPTED'
                }, status=410)
            else:
                get_invitations_adapter().add_message(
                    self.request,
                    messages.ERROR,
                    'invitations/messages/invite_already_accepted.txt',
                    {'email': invitation.email})
                # Redirect to login since there's hopefully an account already.
                return redirect(app_settings.LOGIN_REDIRECT)

        # The key was expired.
        if invitation.key_expired():
            if self.request.is_ajax:
                return JsonResponse({
                    'ok': False,
                    'error': 'KEY_EXPIRED'
                }, status=410)
            else:
                get_invitations_adapter().add_message(
                    self.request,
                    messages.ERROR,
                    'invitations/messages/invite_expired.txt',
                    {'email': invitation.email})
                # Redirect to sign-up since they might be able to register anyway.
                return redirect(self.get_signup_redirect())

        # Authenticated user is required but the user is not authenticated
        if app_settings.INVITATIONS_REQUIRE_VALID_USER and not request.user.is_authenticated():
            if self.request.is_ajax:
                return JsonResponse({
                    'ok': False,
                    'error': 'AUTHENTICATED_USER_REQUIRED'
                }, status=410)
            else:
                get_invitations_adapter().add_message(
                    self.request,
                    messages.ERROR,
                    'invitations/messages/invite_invalid.txt')
                return redirect(app_settings.LOGIN_REDIRECT)

        # The invitation is valid.
        # Mark it as accepted now if ACCEPT_INVITE_AFTER_SIGNUP is False.
        if not app_settings.ACCEPT_INVITE_AFTER_SIGNUP:
            accept_invitation(invitation=invitation,
                              request=self.request,
                              signal_sender=self.__class__)

        get_invitations_adapter().stash_verified_email(
            self.request, invitation.email)

        if self.request.is_ajax:
            return JsonResponse({ 'ok': True })
        else:
            return redirect(self.get_signup_redirect())

    def delete(self, *args, **kwargs):
        invitation = self.get_object()
        invitation.delete()
        return HttpResponse(status=200)

    def get_object(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        try:
            return queryset.get(key=self.kwargs["key"].lower())
        except Invitation.DoesNotExist:
            return None

    def get_queryset(self):
        return Invitation.objects.all()


def accept_invitation(invitation, request, signal_sender):
    invitation.accepted = True
    invitation.save()

    invite_accepted.send(sender=signal_sender, email=invitation.email, request=request)

    get_invitations_adapter().add_message(
        request,
        messages.SUCCESS,
        'invitations/messages/invite_accepted.txt',
        {'email': invitation.email})


def accept_invite_after_signup(sender, request, user, **kwargs):
    invitation = Invitation.objects.filter(email=user.email).first()
    if invitation:
        accept_invitation(invitation=invitation,
                          request=request,
                          signal_sender=Invitation)


if app_settings.ACCEPT_INVITE_AFTER_SIGNUP:
    signed_up_signal = get_invitations_adapter().get_user_signed_up_signal()
    signed_up_signal.connect(accept_invite_after_signup)
