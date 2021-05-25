"""Identification stage logic"""
from dataclasses import asdict
from time import sleep
from typing import Optional

from django.db.models import Q
from django.http import HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from rest_framework.fields import CharField, ListField
from rest_framework.serializers import ValidationError
from structlog.stdlib import get_logger

from authentik.core.models import Application, Source, User
from authentik.core.types import UILoginButtonSerializer
from authentik.flows.challenge import Challenge, ChallengeResponse, ChallengeTypes
from authentik.flows.planner import PLAN_CONTEXT_PENDING_USER
from authentik.flows.stage import (
    PLAN_CONTEXT_PENDING_USER_IDENTIFIER,
    ChallengeStageView,
)
from authentik.flows.views import SESSION_KEY_APPLICATION_PRE
from authentik.stages.identification.models import IdentificationStage

LOGGER = get_logger()


class IdentificationChallenge(Challenge):
    """Identification challenges with all UI elements"""

    user_fields = ListField(child=CharField(), allow_empty=True, allow_null=True)
    application_pre = CharField(required=False)

    enroll_url = CharField(required=False)
    recovery_url = CharField(required=False)
    primary_action = CharField()
    sources = UILoginButtonSerializer(many=True, required=False)

    component = CharField(default="ak-stage-identification")


class IdentificationChallengeResponse(ChallengeResponse):
    """Identification challenge"""

    uid_field = CharField()
    component = CharField(default="ak-stage-identification")

    pre_user: Optional[User] = None

    def validate_uid_field(self, value: str) -> str:
        """Validate that user exists"""
        pre_user = self.stage.get_user(value)
        if not pre_user:
            sleep(0.150)
            LOGGER.debug("invalid_login", identifier=value)
            raise ValidationError("Failed to authenticate.")
        self.pre_user = pre_user
        return value


class IdentificationStageView(ChallengeStageView):
    """Form to identify the user"""

    response_class = IdentificationChallengeResponse

    def get_user(self, uid_value: str) -> Optional[User]:
        """Find user instance. Returns None if no user was found."""
        current_stage: IdentificationStage = self.executor.current_stage
        query = Q()
        for search_field in current_stage.user_fields:
            model_field = search_field
            if current_stage.case_insensitive_matching:
                model_field += "__iexact"
            else:
                model_field += "__exact"
            query |= Q(**{model_field: uid_value})
        users = User.objects.filter(query, is_active=True)
        if users.exists():
            LOGGER.debug("Found user", user=users.first(), query=query)
            return users.first()
        return None

    def get_challenge(self) -> Challenge:
        current_stage: IdentificationStage = self.executor.current_stage
        challenge = IdentificationChallenge(
            data={
                "type": ChallengeTypes.NATIVE.value,
                "primary_action": _("Log in"),
                "component": "ak-stage-identification",
                "user_fields": current_stage.user_fields,
            }
        )
        # If the user has been redirected to us whilst trying to access an
        # application, SESSION_KEY_APPLICATION_PRE is set in the session
        if SESSION_KEY_APPLICATION_PRE in self.request.session:
            challenge.initial_data["application_pre"] = self.request.session.get(
                SESSION_KEY_APPLICATION_PRE, Application()
            ).name
        # Check for related enrollment and recovery flow, add URL to view
        if current_stage.enrollment_flow:
            challenge.initial_data["enroll_url"] = reverse(
                "authentik_core:if-flow",
                kwargs={"flow_slug": current_stage.enrollment_flow.slug},
            )
        if current_stage.recovery_flow:
            challenge.initial_data["recovery_url"] = reverse(
                "authentik_core:if-flow",
                kwargs={"flow_slug": current_stage.recovery_flow.slug},
            )

        # Check all enabled source, add them if they have a UI Login button.
        ui_sources = []
        sources: list[Source] = (
            current_stage.sources.filter(enabled=True)
            .order_by("name")
            .select_subclasses()
        )
        for source in sources:
            ui_login_button = source.ui_login_button
            if ui_login_button:
                button = asdict(ui_login_button)
                button["challenge"] = ui_login_button.challenge.data
                ui_sources.append(button)
        challenge.initial_data["sources"] = ui_sources
        return challenge

    def challenge_valid(
        self, response: IdentificationChallengeResponse
    ) -> HttpResponse:
        self.executor.plan.context[PLAN_CONTEXT_PENDING_USER] = response.pre_user
        current_stage: IdentificationStage = self.executor.current_stage
        if not current_stage.show_matched_user:
            self.executor.plan.context[
                PLAN_CONTEXT_PENDING_USER_IDENTIFIER
            ] = response.validated_data.get("uid_field")
        return self.executor.stage_ok()
