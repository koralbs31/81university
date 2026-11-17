import asyncio
import copy
import logging
import math
import re
from datetime import datetime, timedelta
from typing import Sequence
from uuid import uuid4
try:
    import regex as _regex
except Exception:
    _regex = None
from fastapi import HTTPException, Request, UploadFile
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.exc import IntegrityError
from src.db.courses.activities import Activity
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.assignments import Assignment, AssignmentCreate, AssignmentRead, AssignmentTask, AssignmentTaskCreate, AssignmentTaskOrder, AssignmentTaskRead, AssignmentTaskSubmission, AssignmentTaskSubmissionCreate, AssignmentTaskSubmissionRead, AssignmentTaskSubmissionUpdate, AssignmentTaskTypeEnum, AssignmentTaskUpdate, AssignmentUpdate, AssignmentUserSubmission, AssignmentUserSubmissionCreate, AssignmentUserSubmissionRead, AssignmentUserSubmissionStatus, GradingTypeEnum, SolutionRevealEnum
from src.db.courses.courses import Course
from src.db.organizations import Organization
from src.db.trail_runs import TrailRun
from src.db.trail_steps import TrailStep
from src.db.users import AnonymousUser, PublicUser, User, APITokenUser
from src.security.features_utils.usage import check_limits_with_usage, decrease_feature_usage, increase_feature_usage
from src.security.submission_file_access import submission_files_belong_to
from src.security.rbac import authorization_verify_based_on_roles, authorization_verify_api_token_permissions, check_resource_access, AccessAction
from src.services.courses.activities.access import verify_activity_reader_access
from src.services.courses.activities.uploads.sub_file import upload_submission_file
from src.services.courses.activities.uploads.tasks_ref_files import upload_reference_file
from src.services.courses.activities.uploads.solution_files import upload_solution_file
from src.services.courses.activities.quiz_modes import resolve_grading_mode, resolve_response_type, score_question
from src.services.trail.trail import check_trail_presence
from src.services.courses.certifications import check_course_completion_and_create_certificate, is_course_fully_completed, revoke_user_certificate, sync_trailrun_status, are_course_assignments_passed
from src.services.analytics.analytics import track
from src.services.analytics import events as analytics_events
from src.services.audit.audit import record_audit_event
from src.db.user_audit_events import UserAuditEventType
from src.services.webhooks.dispatch import dispatch_webhooks
from src.core.ee_hooks import notify_ee_course_content_changed, notify_ee_learner_progress
_REGEX_MAX_LEN = 1000
_REGEX_TIMEOUT_SECONDS = 0.5
_THOUSANDS_INT = re.compile('^[+-]?\\d{1,3}(,\\d{3})+$')
_THOUSANDS_DEC = re.compile('^[+-]?\\d{1,3}(,\\d{3})+\\.\\d+$')
logger = logging.getLogger(__name__)

def _block_api_tokens(current_user: PublicUser | AnonymousUser | APITokenUser) -> None:
    """
    Block API tokens from accessing assignments.

    SECURITY: Assignments contain sensitive user submission data and grades.
    API tokens are not allowed to access this data - only user authentication is permitted.

    Still used on the learner-centric, session-only endpoints (the ``/me`` reads,
    student submission upsert, file uploads, retry, "done"). The instructor-style
    endpoints (authoring, reading submissions, grading) instead go through
    ``authorize_assignment_access`` so API tokens with the ``assignments`` rights
    bucket can drive assignments headlessly.
    """
    if isinstance(current_user, APITokenUser):
        raise HTTPException(status_code=403, detail='API tokens cannot access assignments. Only user authentication is allowed.')
_ACCESS_ACTION_TO_TOKEN_ACTION = {AccessAction.CREATE: 'create', AccessAction.READ: 'read', AccessAction.UPDATE: 'update', AccessAction.DELETE: 'delete'}

async def authorize_assignment_access(request: Request, db_session: AsyncSession, current_user: PublicUser | AnonymousUser | APITokenUser, course_uuid: str, access_action: AccessAction, token_action: str | None=None, activity_id: int | None=None) -> None:
    """Authorize an assignment operation for either a user session or an API token.

    With ``activity_id`` a session READ also runs the activity reader gate
    (published, paywall, locks): an assignment is activity content.

    Sessions keep the existing course-scoped RBAC (``check_resource_access``).
    API tokens are authorized against the single ``assignments`` rights bucket,
    with the organization boundary enforced via the parent course UUID.

    ``token_action`` overrides the rights action checked for tokens when it should
    differ from the session ``access_action`` (e.g. listing per-task submissions
    requires instructor UPDATE for a session but is a ``read`` for a token; an
    authoring token attaches reference files as part of ``create``).
    """
    if isinstance(current_user, APITokenUser):
        action = token_action or _ACCESS_ACTION_TO_TOKEN_ACTION[access_action]
        await authorization_verify_api_token_permissions(request, current_user, action, course_uuid, db_session, resource_type_override='assignments')
        return
    await check_resource_access(request, db_session, current_user, course_uuid, access_action)
    if activity_id is not None and access_action == AccessAction.READ:
        row = (await db_session.execute(select(Activity, Course).join(Course, Course.id == Activity.course_id).where(Activity.id == activity_id))).first()
        if row:
            await verify_activity_reader_access(request, row[0], row[1], current_user, db_session)

async def _is_assignment_instructor(request: Request, current_user: PublicUser | AnonymousUser | APITokenUser, course_uuid: str, db_session: AsyncSession) -> bool:
    """Whether the principal has instructor-level (grading) authority on the course.

    An API token that has already passed ``authorize_assignment_access`` for the
    ``assignments`` bucket acts with instructor visibility (only org admins can
    mint such tokens). Sessions fall back to the role check.
    """
    if isinstance(current_user, APITokenUser):
        return True
    return await authorization_verify_based_on_roles(request, current_user.id, 'update', course_uuid, db_session)

def _is_assignment_past_due(assignment: Assignment) -> bool:
    """Return True if the assignment has a due_date set and it is in the past.

    due_date is stored as a free-form string. We parse it defensively: if it
    is empty or unparseable, we treat the deadline as NOT set (return False)
    so a malformed value never locks students out. Comparison is done with a
    naive ``datetime.now()`` to match the rest of this module (all timestamps
    here are naive local-time strings produced by ``datetime.now()``).
    """
    raw = getattr(assignment, 'due_date', None)
    if not raw or not str(raw).strip():
        return False
    raw_str = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(raw_str)
    except (ValueError, TypeError):
        return False
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    if 'T' not in raw_str and ':' not in raw_str:
        parsed = parsed + timedelta(days=1)
    return parsed < datetime.now()

def _strip_answer_key(contents, keep_answer_keys: bool=False):
    """Remove answer-key fields from a task's ``contents`` so students can't read
    the correct answers out of the task GET payload before submitting.

    Grading is server-side, so the client never needs the key. Returns a deep
    copy with the sensitive fields removed per task type:
      - QUIZ:   questions[].options[].assigned_right_answer
      - FORM:   questions[].blanks[].correctAnswer
      - SHORT_ANSWER: correct_answers
      - NUMBER_ANSWER: correct_value (tolerance is harmless without it)
      - CODE:   solution_code, and hidden test cases' stdin/expectedStdout

    ``keep_answer_keys`` is the reveal mode used for a graded student who is
    allowed to see which answers were right (``show_correct_answers``): the
    per-answer keys above are kept, but the CODE reference ``solution_code`` and
    hidden test-case I/O are STILL removed. Those are never appropriate to
    expose to a student; revealing them would hand over the full solution and
    every hidden test, defeating the point of hidden tests entirely.
    """
    if not isinstance(contents, dict):
        return contents
    c = copy.deepcopy(contents)
    for q in c.get('questions') or []:
        if not isinstance(q, dict) or 'response_type' in q:
            continue
        options = q.get('options')
        if isinstance(options, list) and options:
            q['response_type'] = resolve_response_type(q)
    c.pop('solution_code', None)
    test_cases = c.get('test_cases')
    if isinstance(test_cases, list):
        for tc in test_cases:
            if isinstance(tc, dict) and tc.get('hidden'):
                tc.pop('stdin', None)
                tc.pop('expectedStdout', None)
                tc.pop('expected_stdout', None)
    if keep_answer_keys:
        return c
    c.pop('correct_answers', None)
    c.pop('correct_value', None)
    c.pop('explanation', None)
    questions = c.get('questions')
    if isinstance(questions, list):
        for q in questions:
            if not isinstance(q, dict):
                continue
            for opt in q.get('options') or []:
                if isinstance(opt, dict):
                    opt.pop('assigned_right_answer', None)
            for blank in q.get('blanks') or []:
                if isinstance(blank, dict):
                    blank.pop('correctAnswer', None)
    return c

async def _student_may_see_answer_key(current_user, assignment, db_session: AsyncSession) -> bool:
    """A student may see the answer key only after their own submission is GRADED
    AND the teacher opted into ``show_correct_answers``, the same gate the
    student UI uses to reveal answers. Everyone else (pre-grade, opt-out,
    anonymous) gets the key stripped."""
    if not getattr(assignment, 'show_correct_answers', False):
        return False
    if not isinstance(current_user, PublicUser):
        return False
    sub = (await db_session.execute(select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == current_user.id, AssignmentUserSubmission.assignment_id == assignment.id))).scalars().first()
    if not (sub and sub.submission_status == AssignmentUserSubmissionStatus.GRADED):
        return False
    if getattr(assignment, 'allow_retries', False):
        max_retries = int(getattr(assignment, 'max_retries', 0) or 0)
        attempt = int(getattr(sub, 'attempt_number', 1) or 1)
        retries_remain = max_retries == 0 or attempt < max_retries
        if retries_remain:
            return False
    return True
_HANDED_IN_STATUSES = (AssignmentUserSubmissionStatus.SUBMITTED, AssignmentUserSubmissionStatus.LATE, AssignmentUserSubmissionStatus.GRADED)

async def _student_may_see_solution(current_user, assignment, db_session: AsyncSession) -> bool:
    """Whether this (non-instructor) reader has unlocked the model answer.

    The reveal rule is the teacher's ``solution_reveal`` setting measured
    against the reader's OWN submission:

      NEVER          -> never.
      ON_SUBMISSION  -> once their submission is handed in (SUBMITTED / LATE /
                        GRADED). This is the formative case: turn the document
                        in, get the corrige back immediately.
      AFTER_GRADING  -> only once their submission is GRADED.

    One extra guard on a *graded* assignment: while a retry attempt is still
    available, handing over the corrige is a free 100% (read it, hit "Try
    again", resubmit it). So we withhold it until no attempt remains, mirroring
    ``_student_may_see_answer_key``. An ungraded (formative) assignment has no
    score to game, so the retry guard does not apply there: seeing the worked
    solution and trying again is exactly the intended loop.
    """
    reveal = getattr(assignment, 'solution_reveal', None) or SolutionRevealEnum.NEVER
    if reveal == SolutionRevealEnum.NEVER:
        return False
    if not isinstance(current_user, PublicUser):
        return False
    sub = (await db_session.execute(select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == current_user.id, AssignmentUserSubmission.assignment_id == assignment.id))).scalars().first()
    if sub is None:
        return False
    if reveal == SolutionRevealEnum.AFTER_GRADING:
        if sub.submission_status != AssignmentUserSubmissionStatus.GRADED:
            return False
    elif sub.submission_status not in _HANDED_IN_STATUSES:
        return False
    if not getattr(assignment, 'ungraded', False) and getattr(assignment, 'allow_retries', False):
        max_retries = int(getattr(assignment, 'max_retries', 0) or 0)
        attempt = int(getattr(sub, 'attempt_number', 1) or 1)
        retries_remain = max_retries == 0 or attempt < max_retries
        if retries_remain:
            return False
    return True

async def _resolve_solution_visibility(request: Request, db_session: AsyncSession, current_user, course_uuid: str, assignment: Assignment) -> bool:
    """Whether this reader may see the assignment's model answer.

    Short-circuits when the assignment has no corrige at all, so every
    assignment that never uses the feature (which is all of them until a
    teacher opts in) pays neither the instructor-role lookup nor the
    submission query on a read.
    """
    if not ((assignment.solution or '').strip() or assignment.solution_file):
        return False
    if await _is_assignment_instructor(request, current_user, course_uuid, db_session):
        return True
    return await _student_may_see_solution(current_user, assignment, db_session)

def _apply_solution_visibility(result: AssignmentRead, assignment: Assignment, *, unlocked: bool) -> AssignmentRead:
    """Stamp the reveal flags on an outgoing AssignmentRead and, when the
    reader has not unlocked it, strip the corrige from the payload.

    The strip is what actually enforces the reveal; a client-side gate would
    ship the solution to every learner in the assignment GET and lose the whole
    point of the feature.
    """
    result.has_solution = bool((assignment.solution or '').strip() or assignment.solution_file)
    result.solution_unlocked = bool(unlocked)
    if not unlocked:
        result.solution = None
        result.solution_file = None
    return result
DEFAULT_PASSING_THRESHOLD_PERCENTAGE = 50.0
LETTER_PASSING_THRESHOLD_PERCENTAGE = 60.0
AUTO_GRADABLE_TASK_TYPES = frozenset({AssignmentTaskTypeEnum.QUIZ, AssignmentTaskTypeEnum.FORM, AssignmentTaskTypeEnum.CODE, AssignmentTaskTypeEnum.SHORT_ANSWER, AssignmentTaskTypeEnum.NUMBER_ANSWER})
SERVER_VERIFIED_TASK_TYPES = frozenset({AssignmentTaskTypeEnum.SHORT_ANSWER, AssignmentTaskTypeEnum.NUMBER_ANSWER, AssignmentTaskTypeEnum.QUIZ, AssignmentTaskTypeEnum.FORM, AssignmentTaskTypeEnum.CODE})

def _check_short_answer(answer, accepted, mode) -> bool:
    """
    Server-side mirror of TaskShortAnswerObject.tsx > checkShortAnswer.

    Returns True if the student's trimmed answer matches any of the accepted
    answers under the configured match mode. Anchors regex patterns with
    fullmatch so a pattern like ``hello`` doesn't silently match
    ``hello world``. Invalid regex patterns are treated as non-matches
    (never raise).
    """
    trimmed = (str(answer) if answer is not None else '').strip()
    if not trimmed:
        return False
    if not isinstance(accepted, list):
        return False
    match_mode = mode or 'case_insensitive'
    for raw in accepted:
        if not isinstance(raw, str):
            continue
        expected = raw.strip()
        if not expected:
            continue
        if match_mode == 'exact':
            if trimmed == expected:
                return True
        elif match_mode == 'case_insensitive':
            if trimmed.lower() == expected.lower():
                return True
        elif match_mode == 'contains':
            if expected.lower() in trimmed.lower():
                return True
        elif match_mode == 'regex':
            if len(expected) > _REGEX_MAX_LEN or len(trimmed) > _REGEX_MAX_LEN:
                continue
            try:
                if _regex is not None:
                    if _regex.fullmatch(expected, trimmed, flags=_regex.IGNORECASE, timeout=_REGEX_TIMEOUT_SECONDS):
                        return True
                elif re.fullmatch(expected, trimmed, re.IGNORECASE):
                    return True
            except Exception:
                pass
    return False

def _check_number_answer(answer_raw, correct_value, tolerance) -> bool:
    """
    Server-side mirror of TaskNumberAnswerObject.tsx > checkNumberAnswer.

    Parses the student's answer as a float (accepting comma decimals),
    returns True when ``abs(parsed - correct) <= abs(tolerance)``. Returns
    False for blank / NaN / non-numeric input so students can't earn
    credit for a non-answer.
    """
    if answer_raw is None:
        return False
    cleaned = str(answer_raw).strip()
    if not cleaned:
        return False
    if _THOUSANDS_INT.match(cleaned) or _THOUSANDS_DEC.match(cleaned):
        cleaned = cleaned.replace(',', '')
    elif cleaned.count(',') == 1 and '.' not in cleaned:
        cleaned = cleaned.replace(',', '.')
    elif ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace(',', '')
    try:
        parsed = float(cleaned)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(parsed):
        return False
    try:
        correct = float(correct_value if correct_value is not None else 0)
        tol = abs(float(tolerance if tolerance is not None else 0))
    except (TypeError, ValueError):
        return False
    return abs(parsed - correct) <= tol

def _grade_quiz_task(contents: dict, submission_data: dict, task_max: int) -> int:
    """
    Grade a quiz task PER QUESTION (not per option).

    Under the default all-or-nothing grading mode a question is correct only
    when the student's selected option set exactly matches the answer key:
    every option the student marked must equal that option's
    ``assigned_right_answer`` (all correct options selected, no incorrect
    option selected).

    Under ``contents["grading_mode"] == "partial_credit"`` a select-all-that-
    apply question instead scores a fraction; see
    ``quiz_modes.score_question``. Single-response questions score 1 or 0 under
    either mode. The task score is
    ``round(sum(question_scores) / total_questions * task_max)``, which reduces
    to the old ``correct_questions / total_questions`` whenever every question
    scores 0 or 1 (i.e. always, in all-or-nothing mode).

    Per-question is the only defensible model: the previous per-option scoring
    gave phantom credit for non-answers (a blank 4-option question still "matched"
    its 3 unselected wrong options -> 75%), so a learner could leave most of an
    exam blank and still clear a high pass bar. Missing submissions are treated
    as "not selected".

    Returns a grade in [0, task_max], rounded.
    """
    questions = contents.get('questions') or []
    submissions = submission_data.get('submissions') or []
    answer_by_key: dict = {}
    for sub in submissions:
        if not isinstance(sub, dict):
            continue
        q_uuid = sub.get('questionUUID')
        o_uuid = sub.get('optionUUID')
        if q_uuid and o_uuid:
            answer_by_key[q_uuid, o_uuid] = bool(sub.get('answer'))
    grading_mode = resolve_grading_mode(contents)
    total_questions = 0
    earned_score = 0.0
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = [o for o in question.get('options') or [] if isinstance(o, dict)]
        if not options:
            continue
        if not any((bool(o.get('assigned_right_answer')) for o in options)):
            logger.warning('Quiz question %s has no correct option; skipping from auto-grade', question.get('questionUUID', '?'))
            continue
        total_questions += 1
        q_uuid = question.get('questionUUID')
        answers = [(bool(option.get('assigned_right_answer')), answer_by_key.get((q_uuid, option.get('optionUUID')), False)) for option in options]
        earned_score += score_question(answers, resolve_response_type(question), grading_mode)
    if total_questions == 0 or task_max <= 0:
        return 0
    return round(earned_score / total_questions * task_max)

def _grade_form_task(contents: dict, submission_data: dict, task_max: int) -> int:
    """
    Server-side mirror of TaskFormObject.tsx > gradeFC.

    Each blank in each question is worth one point. The comparison is
    case-insensitive and trim-whitespace, matching the client behavior.

    Returns a grade in [0, task_max], rounded.
    """
    questions = contents.get('questions') or []
    submissions = submission_data.get('submissions') or []
    answer_by_key: dict = {}
    for sub in submissions:
        if not isinstance(sub, dict):
            continue
        q_uuid = sub.get('questionUUID')
        b_uuid = sub.get('blankUUID')
        if q_uuid and b_uuid:
            answer_by_key[q_uuid, b_uuid] = sub.get('answer')
    total_blanks = 0
    correct_blanks = 0
    for question in questions:
        if not isinstance(question, dict):
            continue
        q_uuid = question.get('questionUUID')
        blanks = question.get('blanks') or []
        for blank in blanks:
            if not isinstance(blank, dict):
                continue
            b_uuid = blank.get('blankUUID')
            correct_value = blank.get('correctAnswer', '')
            if correct_value is None or not str(correct_value).strip():
                continue
            total_blanks += 1
            student_value = answer_by_key.get((q_uuid, b_uuid), '')
            if student_value is None:
                continue
            if str(student_value).strip().lower() == str(correct_value).strip().lower():
                correct_blanks += 1
    if total_blanks == 0 or task_max <= 0:
        return 0
    return round(correct_blanks / total_blanks * task_max)

def _normalize_code_output(s):
    """
    Normalization for Judge0 stdout comparison. Same logic as the client
    and as the one-off version in code_execution.py: strips trailing
    whitespace per line and drops trailing blank lines so ``print("x")``
    matches ``x`` and Windows line endings don't cause false failures.
    """
    if not s:
        return ''
    lines = [line.rstrip() for line in s.splitlines()]
    while lines and lines[-1] == '':
        lines.pop()
    return '\n'.join(lines)

async def _grade_code_task_async(task, task_submission):
    """
    Re-grade a CODE task server-side by running the student's stored
    source code against the teacher's configured test cases via Judge0.

    Grading modes (mirrors TaskCodeObject.tsx > gradeFC):
    - ``binary``: full marks only when every test passes, else 0.
    - ``custom_weights``: ``round(passed_weight / total_weight * max)``.
    - ``equal_weight`` (default): ``round(passed / total * max)``.

    Returns an int grade in [0, max_grade_value]. Returns ``None`` when the
    grade can't be trusted: Judge0 isn't configured, or *any* test case
    couldn't actually be executed (transport error, timeout, Judge0 internal
    error, or a submission still queued/processing). In that case the caller
    leaves the submission pending rather than finalizing a bogus 0, so a
    Judge0 outage never zeroes a student out on correct work.
    """
    from src.routers.code_execution import _get_judge0_config, _submit_single
    if task_submission is None:
        return 0
    contents = task.contents or {}
    submission_data = task_submission.task_submission or {}
    source_code = submission_data.get('source_code', '') or ''
    if not source_code.strip():
        return 0
    language_id = submission_data.get('language_id') or contents.get('language_id')
    if language_id is None:
        return 0
    test_cases = contents.get('test_cases') or []
    if not test_cases:
        return 0
    task_max = int(task.max_grade_value or 0)
    if task_max <= 0:
        return 0
    try:
        judge0_cfg = _get_judge0_config()
    except HTTPException:
        logger.warning('Judge0 not configured; skipping server-side CODE grading for task %s', getattr(task, 'assignment_task_uuid', '?'))
        return None
    JUDGE0_UNEXECUTED_STATUS_IDS = {1, 2, 13, 14}

    async def run_one(tc):
        stdin = tc.get('stdin') or ''
        expected = tc.get('expectedStdout') or tc.get('expected_stdout') or ''
        try:
            r = await _submit_single(judge0_cfg, int(language_id), source_code, stdin)
        except Exception:
            logger.exception('Judge0 call failed during CODE grading for task %s', getattr(task, 'assignment_task_uuid', '?'))
            return (tc, None)
        status = r.get('status') or {}
        status_id = status.get('id')
        if status_id in JUDGE0_UNEXECUTED_STATUS_IDS:
            logger.warning('Judge0 returned non-terminal/internal status %s during CODE grading for task %s; treating as unexecuted', status_id, getattr(task, 'assignment_task_uuid', '?'))
            return (tc, None)
        actual = _normalize_code_output(r.get('stdout'))
        passed = status_id == 3 and actual == _normalize_code_output(expected)
        return (tc, passed)
    results = await asyncio.gather(*(run_one(tc) for tc in test_cases))
    if any((passed is None for _, passed in results)):
        logger.warning('CODE grading incomplete for task %s (one or more tests unexecuted); leaving submission pending', getattr(task, 'assignment_task_uuid', '?'))
        return None
    passed_count = sum((1 for _, passed in results if passed))
    total_count = len(results)
    grading_mode = contents.get('grading_mode') or 'equal_weight'
    if grading_mode == 'binary':
        return task_max if total_count > 0 and passed_count == total_count else 0
    if grading_mode == 'custom_weights':
        total_weight = sum((int(tc.get('weight') or 1) for tc in test_cases))
        passed_weight = sum((int(tc.get('weight') or 1) for tc, passed in results if passed))
        if total_weight <= 0:
            return 0
        return round(passed_weight / total_weight * task_max)
    return round(passed_count / total_count * task_max)

async def _server_verified_task_grade(task, task_submission):
    """
    If this task type is in SERVER_VERIFIED_TASK_TYPES, re-compute its
    grade from the stored task contents + submission data and return it.
    Returns ``None`` for task types we don't verify, or when the CODE
    grader can't reach Judge0; the caller should fall back to
    ``task_submission.grade`` in both cases.
    """
    if task.assignment_type not in SERVER_VERIFIED_TASK_TYPES:
        return None
    if task_submission is None:
        return 0
    contents = task.contents or {}
    submission_data = task_submission.task_submission or {}
    task_max = int(task.max_grade_value or 0)
    if task.assignment_type == AssignmentTaskTypeEnum.SHORT_ANSWER:
        passed = _check_short_answer(submission_data.get('answer'), contents.get('correct_answers', []), contents.get('match_mode'))
        return task_max if passed else 0
    if task.assignment_type == AssignmentTaskTypeEnum.NUMBER_ANSWER:
        passed = _check_number_answer(submission_data.get('answer'), contents.get('correct_value'), contents.get('tolerance'))
        return task_max if passed else 0
    if task.assignment_type == AssignmentTaskTypeEnum.QUIZ:
        return _grade_quiz_task(contents, submission_data, task_max)
    if task.assignment_type == AssignmentTaskTypeEnum.FORM:
        return _grade_form_task(contents, submission_data, task_max)
    if task.assignment_type == AssignmentTaskTypeEnum.CODE:
        return await _grade_code_task_async(task, task_submission)
    return None

def _percentage_to_letter_grade(percentage: float) -> str:
    """Convert a 0-100 percentage into a US-style A/B/C/D/F letter grade."""
    if percentage >= 90:
        return 'A'
    if percentage >= 80:
        return 'B'
    if percentage >= 70:
        return 'C'
    if percentage >= 60:
        return 'D'
    return 'F'

def _percentage_to_gpa(percentage: float) -> str:
    """Convert a 0-100 percentage into a US 4.0 GPA scale string."""
    if percentage >= 93:
        return '4.0'
    if percentage >= 90:
        return '3.7'
    if percentage >= 87:
        return '3.3'
    if percentage >= 83:
        return '3.0'
    if percentage >= 80:
        return '2.7'
    if percentage >= 77:
        return '2.3'
    if percentage >= 73:
        return '2.0'
    if percentage >= 70:
        return '1.7'
    if percentage >= 67:
        return '1.3'
    if percentage >= 63:
        return '1.0'
    if percentage >= 60:
        return '0.7'
    return '0.0'

def _build_tasks_breakdown(assignment_tasks, task_submissions_by_task_id: dict, passing_threshold: float) -> list:
    """
    Build the per-task breakdown array included in grade responses.

    Shared by the grading path (``_apply_grade_and_finalize``) and the read
    path (``get_grade_assignment_submission``) so both sides always agree on
    the numbers. Each row includes the raw grade, the task's max, the
    percentage, and a ``passed`` flag computed against the assignment's
    grading-type-aware passing threshold. That way the student's activity
    view and the teacher's evaluate modal can render consistent pass/fail
    chips without each one re-deriving its own threshold.
    """
    rows = []
    for index, task in enumerate(sorted(assignment_tasks, key=lambda t: t.id or 0)):
        ts = task_submissions_by_task_id.get(task.id)
        task_max = int(task.max_grade_value or 0)
        task_raw = int(ts.grade or 0) if ts else 0
        task_percentage = round(task_raw / task_max * 100.0, 2) if task_max > 0 else 0.0
        task_percentage = max(min(task_percentage, 100.0), 0.0)
        rows.append({'index': index + 1, 'assignment_task_uuid': task.assignment_task_uuid, 'title': task.title, 'description': task.description, 'assignment_type': task.assignment_type, 'submitted': ts is not None, 'grade': task_raw, 'max_grade': task_max, 'percentage': task_percentage, 'percentage_display': f'{task_percentage:.0f}%', 'points_summary': f'{task_raw}/{task_max}', 'passed': task_percentage >= passing_threshold, 'feedback': ts.task_submission_grade_feedback if ts else None, 'manually_graded': bool(ts.manually_graded) if ts else False})
    return rows

def compute_assignment_grade(raw_grade: int, max_grade: int, grading_type: GradingTypeEnum | str | None, overall_feedback: str | None=None, pass_threshold_percentage: float | None=None) -> dict:
    """
    Build a normalized grade object from a raw grade sum and the configured
    grading type.

    Responsibilities:
    - Clamp raw_grade to [0, max_grade] so a buggy task sum can't report 120/100.
    - Guard against max_grade <= 0 (yields a 0% grade, not a divide-by-zero).
    - Compute a single percentage that every display format derives from.
    - Produce a human-readable `display_grade` (the canonical string the UI
      renders), plus `letter_grade`, `points_summary`, and `percentage_display`
      as secondary formats the UI can show side-by-side without doing math.
    - Flag `passed` using a mode-aware threshold so it stays consistent with
      the display: ALPHABET/GPA_SCALE pass at 60% (D / 0.7), everything else
      passes at 50%.

    The backend intentionally stores only the raw integer sum in
    AssignmentUserSubmission.grade; all formatting is derived on read.
    """
    clamped_max = max(int(max_grade or 0), 0)
    clamped_grade = max(min(int(raw_grade or 0), clamped_max), 0)
    if clamped_max > 0:
        percentage = clamped_grade / clamped_max * 100.0
    else:
        percentage = 0.0
    percentage = round(percentage, 2)
    gt_value = grading_type.value if isinstance(grading_type, GradingTypeEnum) else grading_type or 'NUMERIC'
    if gt_value in ('ALPHABET', 'GPA_SCALE'):
        passing_threshold = LETTER_PASSING_THRESHOLD_PERCENTAGE
    else:
        passing_threshold = DEFAULT_PASSING_THRESHOLD_PERCENTAGE
    if pass_threshold_percentage is not None:
        passing_threshold = max(0.0, min(float(pass_threshold_percentage), 100.0))
    passed = percentage >= passing_threshold
    letter_grade = _percentage_to_letter_grade(percentage)
    points_summary = f'{clamped_grade}/{clamped_max} pts'
    percentage_display = f'{percentage:.2f}%'
    if gt_value == 'ALPHABET':
        if not passed:
            display_grade = 'F'
        elif letter_grade == 'F':
            display_grade = 'D'
        else:
            display_grade = letter_grade
    elif gt_value == 'PERCENTAGE':
        display_grade = percentage_display
    elif gt_value == 'PASS_FAIL':
        display_grade = 'Pass' if passed else 'Fail'
    elif gt_value == 'GPA_SCALE':
        gpa = _percentage_to_gpa(percentage)
        if not passed:
            display_grade = '0.0'
        elif gpa == '0.0':
            display_grade = '1.0'
        else:
            display_grade = gpa
    else:
        display_grade = f'{round(percentage)}/100'
    return {'grade': clamped_grade, 'max_grade': clamped_max, 'percentage': percentage, 'display_grade': display_grade, 'letter_grade': letter_grade, 'points_summary': points_summary, 'percentage_display': percentage_display, 'passed': passed, 'passing_threshold': passing_threshold, 'grading_type': gt_value, 'overall_feedback': overall_feedback}

async def create_assignment(request: Request, assignment_object: AssignmentCreate, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Course).where(Course.id == assignment_object.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.CREATE)
    await check_limits_with_usage('assignments', course.org_id, db_session)
    parent_activity = (await db_session.execute(select(Activity).where(Activity.id == assignment_object.activity_id))).scalars().first()
    if parent_activity is None or parent_activity.course_id != course.id or parent_activity.org_id != course.org_id:
        raise HTTPException(status_code=400, detail='Activity does not belong to the target course')
    assignment = Assignment(**assignment_object.model_dump())
    if assignment.ungraded:
        assignment.auto_grading = False
    assignment.assignment_uuid = str(f'assignment_{uuid4()}')
    assignment.creation_date = str(datetime.now())
    assignment.update_date = str(datetime.now())
    assignment.org_id = course.org_id
    assignment.course_id = course.id
    assignment.activity_id = parent_activity.id
    parent_chapter_id = (await db_session.execute(select(ChapterActivity.chapter_id).where(ChapterActivity.activity_id == parent_activity.id))).scalars().first()
    if parent_chapter_id is not None:
        assignment.chapter_id = parent_chapter_id
    db_session.add(assignment)
    await db_session.commit()
    await db_session.refresh(assignment)
    await increase_feature_usage('assignments', course.org_id, db_session)
    return _apply_solution_visibility(AssignmentRead.model_validate(assignment), assignment, unlocked=True)

async def read_assignment(request: Request, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment, Course.course_uuid, Activity.activity_uuid).join(Course, Course.id == Assignment.course_id).join(Activity, Activity.id == Assignment.activity_id).where(Assignment.assignment_uuid == assignment_uuid)
    row = (await db_session.execute(statement)).first()
    if not row:
        raise HTTPException(status_code=404, detail='Assignment not found')
    assignment, course_uuid, activity_uuid = row
    await authorize_assignment_access(request, db_session, current_user, course_uuid, AccessAction.READ, activity_id=assignment.activity_id)
    result = AssignmentRead.model_validate(assignment)
    result.course_uuid = course_uuid
    result.activity_uuid = activity_uuid
    unlocked = await _resolve_solution_visibility(request, db_session, current_user, course_uuid, assignment)
    return _apply_solution_visibility(result, assignment, unlocked=unlocked)

async def read_assignment_from_activity_uuid(request: Request, activity_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment, Course.course_uuid, Activity.activity_uuid).join(Activity, Activity.id == Assignment.activity_id).join(Course, Course.id == Assignment.course_id).where(Activity.activity_uuid == activity_uuid)
    row = (await db_session.execute(statement)).first()
    if not row:
        raise HTTPException(status_code=404, detail='Assignment not found')
    assignment, course_uuid, activity_uuid_val = row
    await authorize_assignment_access(request, db_session, current_user, course_uuid, AccessAction.READ, activity_id=assignment.activity_id)
    result = AssignmentRead.model_validate(assignment)
    result.course_uuid = course_uuid
    result.activity_uuid = activity_uuid_val
    unlocked = await _resolve_solution_visibility(request, db_session, current_user, course_uuid, assignment)
    return _apply_solution_visibility(result, assignment, unlocked=unlocked)

async def update_assignment(request: Request, assignment_uuid: str, assignment_object: AssignmentUpdate, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    IMMUTABLE_FIELDS = frozenset({'org_id', 'course_id', 'chapter_id', 'activity_id'})
    CLEARABLE_FIELDS = frozenset({'pass_threshold_percentage', 'due_date'})
    provided = getattr(assignment_object, 'model_fields_set', set())
    for var, value in vars(assignment_object).items():
        if var in IMMUTABLE_FIELDS:
            continue
        if value is not None:
            setattr(assignment, var, value)
        elif var in CLEARABLE_FIELDS and var in provided:
            setattr(assignment, var, None)
    if assignment.ungraded:
        assignment.auto_grading = False
    if assignment_object.published is not None:
        activity = (await db_session.execute(select(Activity).where(Activity.id == assignment.activity_id))).scalars().first()
        if activity:
            activity.published = bool(assignment_object.published)
            db_session.add(activity)
    assignment.update_date = str(datetime.now())
    db_session.add(assignment)
    await db_session.commit()
    await db_session.refresh(assignment)
    notify_ee_course_content_changed(course.id)
    return _apply_solution_visibility(AssignmentRead.model_validate(assignment), assignment, unlocked=True)

async def put_assignment_solution_file(request: Request, db_session: AsyncSession, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, solution_file: UploadFile | None=None):
    """Attach (or replace) the assignment's model-answer document.

    Mirrors ``put_assignment_task_reference_file`` but at the assignment level.
    Instructor-only: the stored filename is the corrige, and handing it to a
    learner before the reveal rule unlocks it would defeat the whole feature.
    """
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Activity).where(Activity.id == assignment.activity_id)
    activity = (await db_session.execute(statement)).scalars().first()
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    org_statement = select(Organization).where(Organization.id == course.org_id)
    org = (await db_session.execute(org_statement)).scalars().first()
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE, token_action='create')
    if not (solution_file and solution_file.filename):
        raise HTTPException(status_code=400, detail='No solution file provided')
    if not (activity and org):
        raise HTTPException(status_code=404, detail='Activity not found')
    name_in_disk = await upload_solution_file(solution_file, activity.activity_uuid, org.org_uuid, course.course_uuid, assignment.assignment_uuid)
    assignment.solution_file = name_in_disk
    assignment.update_date = str(datetime.now())
    db_session.add(assignment)
    await db_session.commit()
    await db_session.refresh(assignment)
    return _apply_solution_visibility(AssignmentRead.model_validate(assignment), assignment, unlocked=True)

async def delete_assignment_solution_file(request: Request, db_session: AsyncSession, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser):
    """Detach the model-answer document from the assignment.

    Only the reference is dropped; the uploaded blob is left in place, matching
    how task reference files behave when replaced.
    """
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    assignment.solution_file = None
    assignment.update_date = str(datetime.now())
    db_session.add(assignment)
    await db_session.commit()
    await db_session.refresh(assignment)
    return _apply_solution_visibility(AssignmentRead.model_validate(assignment), assignment, unlocked=True)

async def delete_assignment(request: Request, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.DELETE)
    await decrease_feature_usage('assignments', course.org_id, db_session)
    await db_session.delete(assignment)
    await db_session.commit()
    notify_ee_course_content_changed(course.id)
    return {'message': 'Assignment deleted'}

async def delete_assignment_from_activity_uuid(request: Request, activity_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Activity).where(Activity.activity_uuid == activity_uuid)
    activity = (await db_session.execute(statement)).scalars().first()
    if not activity:
        raise HTTPException(status_code=404, detail='Activity not found')
    statement = select(Course).where(Course.id == activity.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    statement = select(Assignment).where(Assignment.activity_id == activity.id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.DELETE)
    await decrease_feature_usage('assignments', course.org_id, db_session)
    await db_session.delete(assignment)
    await db_session.commit()
    return {'message': 'Assignment deleted'}

async def create_assignment_task(request: Request, assignment_uuid: str, assignment_task_object: AssignmentTaskCreate, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.CREATE)
    assignment_task = AssignmentTask(**assignment_task_object.model_dump())
    assignment_task.assignment_task_uuid = str(f'assignmenttask_{uuid4()}')
    assignment_task.creation_date = str(datetime.now())
    assignment_task.update_date = str(datetime.now())
    assignment_task.org_id = course.org_id
    assignment_task.chapter_id = assignment.chapter_id
    assignment_task.activity_id = assignment.activity_id
    assignment_task.assignment_id = assignment.id
    assignment_task.course_id = assignment.course_id
    statement = select(func.max(AssignmentTask.order), func.count(AssignmentTask.id)).where(AssignmentTask.assignment_id == assignment.id)
    max_order, task_count = (await db_session.execute(statement)).one()
    assignment_task.order = max(max_order + 1, task_count) if max_order is not None else task_count
    db_session.add(assignment_task)
    await db_session.commit()
    await db_session.refresh(assignment_task)
    return AssignmentTaskRead.model_validate(assignment_task)

async def read_assignment_tasks(request: Request, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    statement = select(AssignmentTask).where(AssignmentTask.assignment_id == assignment.id).order_by(AssignmentTask.order.asc().nulls_last(), AssignmentTask.id.asc())
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ, activity_id=assignment.activity_id)
    is_instructor = await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)
    reveal_to_student = not is_instructor and await _student_may_see_answer_key(current_user, assignment, db_session)
    result = []
    for assignment_task in (await db_session.execute(statement)).scalars().all():
        read = AssignmentTaskRead.model_validate(assignment_task)
        if not is_instructor:
            read.contents = _strip_answer_key(read.contents, keep_answer_keys=reveal_to_student)
        result.append(read)
    return result

async def read_assignment_task(request: Request, assignment_task_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignmenttask = (await db_session.execute(statement)).scalars().first()
    if not assignmenttask:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignmenttask.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ, activity_id=assignment.activity_id)
    read = AssignmentTaskRead.model_validate(assignmenttask)
    is_instructor = await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)
    if not is_instructor:
        reveal_to_student = await _student_may_see_answer_key(current_user, assignment, db_session)
        read.contents = _strip_answer_key(read.contents, keep_answer_keys=reveal_to_student)
    return read

async def put_assignment_task_reference_file(request: Request, db_session: AsyncSession, assignment_task_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, reference_file: UploadFile | None=None):
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Activity).where(Activity.id == assignment.activity_id)
    activity = (await db_session.execute(statement)).scalars().first()
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    org_statement = select(Organization).where(Organization.id == course.org_id)
    org = (await db_session.execute(org_statement)).scalars().first()
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE, token_action='create')
    if reference_file and reference_file.filename and activity and org:
        name_in_disk = await upload_reference_file(reference_file, activity.activity_uuid, org.org_uuid, course.course_uuid, assignment.assignment_uuid, assignment_task_uuid)
        assignment_task.reference_file = name_in_disk
    assignment_task.update_date = str(datetime.now())
    db_session.add(assignment_task)
    await db_session.commit()
    await db_session.refresh(assignment_task)
    return AssignmentTaskRead.model_validate(assignment_task)

async def put_assignment_task_submission_file(request: Request, db_session: AsyncSession, assignment_task_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, sub_file: UploadFile | None=None):
    _block_api_tokens(current_user)
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Activity).where(Activity.id == assignment.activity_id)
    activity = (await db_session.execute(statement)).scalars().first()
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    org_statement = select(Organization).where(Organization.id == course.org_id)
    org = (await db_session.execute(org_statement)).scalars().first()
    if activity:
        await verify_activity_reader_access(request, activity, course, current_user, db_session)
    else:
        await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    if not await authorization_verify_based_on_roles(request, current_user.id, 'read', course.course_uuid, db_session):
        raise HTTPException(status_code=403, detail='You must be enrolled in this course to submit files')
    is_instructor = await authorization_verify_based_on_roles(request, current_user.id, 'update', course.course_uuid, db_session)
    if not is_instructor and _is_assignment_past_due(assignment):
        raise HTTPException(status_code=403, detail='Assignment deadline has passed')
    if sub_file and sub_file.filename and activity and org:
        name_in_disk = await upload_submission_file(sub_file, activity.activity_uuid, org.org_uuid, course.course_uuid, assignment.assignment_uuid, assignment_task_uuid, current_user.id)
        return {'file_uuid': name_in_disk}

async def update_assignment_task(request: Request, assignment_task_uuid: str, assignment_task_object: AssignmentTaskUpdate, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    _SCORING_FIELDS = ('assignment_type', 'contents', 'max_grade_value')
    scoring_changed = any((getattr(assignment_task_object, f, None) is not None and getattr(assignment_task_object, f) != getattr(assignment_task, f) for f in _SCORING_FIELDS))
    for var, value in vars(assignment_task_object).items():
        if value is not None:
            setattr(assignment_task, var, value)
    assignment_task.update_date = str(datetime.now())
    db_session.add(assignment_task)
    await db_session.commit()
    await db_session.refresh(assignment_task)
    if scoring_changed:
        await _regrade_graded_submissions(assignment=assignment, course=course, db_session=db_session, request=request)
    return AssignmentTaskRead.model_validate(assignment_task)

async def reorder_assignment_tasks(request: Request, assignment_uuid: str, order_object: AssignmentTaskOrder, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    statement = select(AssignmentTask).where(AssignmentTask.assignment_id == assignment.id)
    tasks = (await db_session.execute(statement)).scalars().all()
    tasks_by_uuid = {task.assignment_task_uuid: task for task in tasks}
    if len(order_object.task_uuids) != len(tasks_by_uuid) or set(order_object.task_uuids) != set(tasks_by_uuid):
        raise HTTPException(status_code=400, detail='Task order must list every task of the assignment exactly once')
    for position, task_uuid in enumerate(order_object.task_uuids):
        task = tasks_by_uuid[task_uuid]
        if task.order != position:
            task.order = position
            db_session.add(task)
    await db_session.commit()
    return await read_assignment_tasks(request, assignment_uuid, current_user, db_session)

async def delete_assignment_task(request: Request, assignment_task_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.DELETE)
    await db_session.delete(assignment_task)
    await db_session.commit()
    await _regrade_graded_submissions(assignment=assignment, course=course, db_session=db_session, request=request)
    return {'message': 'Assignment Task deleted'}

async def _reconcile_certificate_after_grade_change(request: Request | None, user_id: int, course: Course, db_session: AsyncSession) -> None:
    """Bring a learner's certificate back in line with their current grades.

    Recomputing a grade can flip a learner across the passing threshold. If they
    no longer pass every gating assignment, a held certificate is revoked and the
    enrollment status demoted; if they now pass and the course is otherwise
    complete, a certificate is (re)issued. Best-effort: never abort the caller.
    """
    if not course.id:
        return
    try:
        passed = await are_course_assignments_passed(user_id, course.id, db_session)
        if not passed:
            await revoke_user_certificate(user_id, course.id, db_session, reason='regraded_below_threshold')
            await sync_trailrun_status(user_id, course.id, db_session)
        elif request is not None:
            await check_course_completion_and_create_certificate(request, user_id, course.id, db_session)
    except Exception:
        logger.exception('Failed to reconcile certificate for user %s on course %s after a grade change', user_id, course.id)

async def _regrade_graded_submissions(assignment: Assignment, course: Course, db_session: AsyncSession, request: Request | None=None) -> None:
    """Recompute stored grades for every GRADED submission of ``assignment``.

    Call after the task set changes, so the persisted numerator stops
    disagreeing with the live denominator. Best-effort per learner: one
    learner's failure must not abort the teacher's edit.

    Recomputing can move a learner across the passing threshold, so each
    regraded learner's certificate is reconciled: revoked if they now fail,
    reissued if they now pass. Pass ``request`` to enable reissue.
    """
    graded = (await db_session.execute(select(AssignmentUserSubmission).where(AssignmentUserSubmission.assignment_id == assignment.id, AssignmentUserSubmission.submission_status == AssignmentUserSubmissionStatus.GRADED))).scalars().all()
    for submission in graded:
        if submission.user_id is None:
            continue
        try:
            await _apply_grade_and_finalize(assignment=assignment, course=course, user_id=submission.user_id, assignment_user_submission=submission, db_session=db_session, overall_feedback=None, auto_graded=True, dispatch_webhook=False)
        except Exception:
            logger.exception('Failed to recompute grade for user %s on assignment %s after a task change', submission.user_id, assignment.assignment_uuid)
            continue
        await _reconcile_certificate_after_grade_change(request, submission.user_id, course, db_session)

async def _resolve_token_submission_user(request: Request, db_session: AsyncSession, current_user: PublicUser | AnonymousUser | APITokenUser, course, on_behalf_of_user_id: int | None):
    """Resolve the learner an API token is submitting on behalf of.

    Submit-on-behalf lets a headless/custom frontend write a learner's answer
    via a token. The token must hold ``assignments.create`` and pass an explicit
    ``on_behalf_of_user_id``; the target learner must already be a member of the
    token's organization (learners are referenced by existing LearnHouse id).

    Returns the learner as a ``PublicUser`` to act as, or ``None`` for non-token
    callers (sessions act as themselves).
    """
    if not isinstance(current_user, APITokenUser):
        return None
    if on_behalf_of_user_id is None:
        raise HTTPException(status_code=400, detail='API tokens must set on_behalf_of_user_id to submit on behalf of a learner')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.CREATE)
    from src.security.org_auth import is_org_member
    learner = (await db_session.execute(select(User).where(User.id == on_behalf_of_user_id))).scalars().first()
    if not learner:
        raise HTTPException(status_code=404, detail='Learner not found')
    if not await is_org_member(learner.id, course.org_id, db_session):
        raise HTTPException(status_code=403, detail='Learner is not a member of this organization')
    return PublicUser(**learner.model_dump())
_ASSIGNMENT_TASK_SUBMISSION_MUTABLE_FIELDS = {'task_submission', 'grade', 'task_submission_grade_feedback', 'manually_graded'}

async def handle_assignment_task_submission(request: Request, assignment_task_uuid: str, assignment_task_submission_object: AssignmentTaskSubmissionUpdate, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession, on_behalf_of_user_id: int | None=None):
    assignment_task_submission_uuid = assignment_task_submission_object.assignment_task_submission_uuid
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    token_submitter = await _resolve_token_submission_user(request, db_session, current_user, course, on_behalf_of_user_id)
    if token_submitter is not None:
        submitter = token_submitter
        is_instructor = False
        is_token_submit = True
    else:
        _block_api_tokens(current_user)
        submitter = current_user
        is_instructor = await authorization_verify_based_on_roles(request, current_user.id, 'update', course.course_uuid, db_session)
        is_token_submit = False
    if not is_instructor:
        if not submission_files_belong_to(assignment_task_submission_object.task_submission, submitter.id):
            raise HTTPException(status_code=403, detail='You can only submit files you uploaded')
        if not is_token_submit:
            if not await authorization_verify_based_on_roles(request, current_user.id, 'read', course.course_uuid, db_session):
                raise HTTPException(status_code=403, detail='You must be enrolled in this course to submit assignments')
            if _is_assignment_past_due(assignment):
                raise HTTPException(status_code=403, detail='Assignment deadline has passed')
            existing_user_submission = (await db_session.execute(select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == current_user.id, AssignmentUserSubmission.assignment_id == assignment.id))).scalars().first()
            if existing_user_submission is not None and existing_user_submission.submission_status not in (AssignmentUserSubmissionStatus.PENDING, AssignmentUserSubmissionStatus.NOT_SUBMITTED):
                raise HTTPException(status_code=403, detail='This assignment has already been handed in. Use retry to attempt it again.')
        if assignment_task_submission_object.grade is not None and assignment_task_submission_object.grade != 0 or (assignment_task_submission_object.task_submission_grade_feedback is not None and assignment_task_submission_object.task_submission_grade_feedback != ''):
            raise HTTPException(status_code=403, detail='You do not have permission to update grades')
        assignment_task_submission_object.grade = None
        assignment_task_submission_object.task_submission_grade_feedback = None
        assignment_task_submission_object.assignment_task_id = None
        assignment_task_submission_object.assignment_type = None
        assignment_task_submission_object.manually_graded = False
        if not is_token_submit:
            activity = (await db_session.execute(select(Activity).where(Activity.id == assignment.activity_id))).scalars().first()
            if activity:
                await verify_activity_reader_access(request, activity, course, current_user, db_session)
            else:
                await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    else:
        await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    if assignment_task_submission_uuid:
        statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_submission_uuid == assignment_task_submission_uuid, AssignmentTaskSubmission.assignment_task_id == assignment_task.id)
        assignment_task_submission = (await db_session.execute(statement)).scalars().first()
        if not assignment_task_submission:
            raise HTTPException(status_code=404, detail='Assignment Task Submission not found')
    elif is_instructor and (assignment_task_submission_object.grade is not None and assignment_task_submission_object.grade != 0 or (assignment_task_submission_object.task_submission_grade_feedback is not None and assignment_task_submission_object.task_submission_grade_feedback != '')):
        raise HTTPException(status_code=400, detail='Cannot grade this task: the learner has no submission for it. A target submission is required to record a grade.')
    else:
        statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_id == assignment_task.id, AssignmentTaskSubmission.user_id == submitter.id)
        assignment_task_submission = (await db_session.execute(statement)).scalars().first()
    if assignment_task_submission:
        if not is_instructor and assignment_task_submission.user_id != submitter.id:
            raise HTTPException(status_code=403, detail='You can only update your own submissions')
        for var, value in vars(assignment_task_submission_object).items():
            if value is not None and var in _ASSIGNMENT_TASK_SUBMISSION_MUTABLE_FIELDS:
                setattr(assignment_task_submission, var, value)
        assignment_task_submission.update_date = str(datetime.now())
        db_session.add(assignment_task_submission)
        await db_session.commit()
        await db_session.refresh(assignment_task_submission)
    else:
        current_time = str(datetime.now())
        model_data = assignment_task_submission_object.model_dump()
        assignment_task_submission = AssignmentTaskSubmission(assignment_task_submission_uuid=assignment_task_submission_uuid or f'assignmenttasksubmission_{uuid4()}', task_submission=model_data['task_submission'], grade=0, task_submission_grade_feedback='', assignment_task_id=int(assignment_task.id), assignment_type=assignment_task.assignment_type, activity_id=assignment.activity_id, course_id=assignment.course_id, chapter_id=assignment.chapter_id, user_id=submitter.id, creation_date=current_time, update_date=current_time)
        db_session.add(assignment_task_submission)
        try:
            await db_session.commit()
            await db_session.refresh(assignment_task_submission)
        except IntegrityError:
            await db_session.rollback()
            existing = (await db_session.execute(select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_id == assignment_task.id, AssignmentTaskSubmission.user_id == submitter.id))).scalars().first()
            if existing is None:
                raise
            for var, value in vars(assignment_task_submission_object).items():
                if value is not None and var in _ASSIGNMENT_TASK_SUBMISSION_MUTABLE_FIELDS:
                    setattr(existing, var, value)
            existing.update_date = str(datetime.now())
            db_session.add(existing)
            await db_session.commit()
            await db_session.refresh(existing)
            assignment_task_submission = existing
    return AssignmentTaskSubmissionRead.model_validate(assignment_task_submission)

async def read_user_assignment_task_submissions(request: Request, assignment_task_uuid: str, user_id: int, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    is_instructor = await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)
    if not is_instructor and int(user_id) != int(current_user.id):
        raise HTTPException(status_code=403, detail='You can only view your own submissions')
    statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_id == assignment_task.id, AssignmentTaskSubmission.user_id == user_id)
    assignment_task_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_task_submission:
        raise HTTPException(status_code=404, detail='Assignment Task Submission not found')
    return AssignmentTaskSubmissionRead.model_validate(assignment_task_submission)

async def read_user_assignment_task_submissions_me_batch(request: Request, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    """Return a map of {assignment_task_uuid: submission | None} for the
    current user across every task in the assignment, in a single round trip.
    Replaces N per-task /submissions/me calls from the activity view."""
    _block_api_tokens(current_user)
    assignment_row = (await db_session.execute(select(Assignment, Course.course_uuid).join(Course, Course.id == Assignment.course_id).where(Assignment.assignment_uuid == assignment_uuid))).first()
    if not assignment_row:
        raise HTTPException(status_code=404, detail='Assignment not found')
    assignment, course_uuid = assignment_row
    await check_resource_access(request, db_session, current_user, course_uuid, AccessAction.READ)
    rows = (await db_session.execute(select(AssignmentTask, AssignmentTaskSubmission).outerjoin(AssignmentTaskSubmission, (AssignmentTaskSubmission.assignment_task_id == AssignmentTask.id) & (AssignmentTaskSubmission.user_id == current_user.id)).where(AssignmentTask.assignment_id == assignment.id).order_by(AssignmentTaskSubmission.id.asc()))).all()
    return {task.assignment_task_uuid: AssignmentTaskSubmissionRead.model_validate(sub) if sub else None for task, sub in rows}

async def read_user_assignment_task_submissions_me(request: Request, assignment_task_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    _block_api_tokens(current_user)
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_id == assignment_task.id, AssignmentTaskSubmission.user_id == current_user.id)
    assignment_task_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_task_submission:
        return None
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    return AssignmentTaskSubmissionRead.model_validate(assignment_task_submission)

async def read_assignment_task_submissions(request: Request, assignment_task_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession, limit: int=50, offset: int=0):
    statement = select(AssignmentTask).where(AssignmentTask.assignment_task_uuid == assignment_task_uuid)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE, token_action='read')
    statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_id == assignment_task.id).order_by(AssignmentTaskSubmission.id.desc()).limit(limit).offset(offset)
    submissions = (await db_session.execute(statement)).scalars().all()
    return [AssignmentTaskSubmissionRead.model_validate(s) for s in submissions]

async def update_assignment_task_submission(request: Request, assignment_task_submission_uuid: str, assignment_task_submission_object: AssignmentTaskSubmissionCreate, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    _block_api_tokens(current_user)
    statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_submission_uuid == assignment_task_submission_uuid)
    assignment_task_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_task_submission:
        raise HTTPException(status_code=404, detail='Assignment Task Submission not found')
    statement = select(AssignmentTask).where(AssignmentTask.id == assignment_task_submission.assignment_task_id)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    is_instructor = await authorization_verify_based_on_roles(request, current_user.id, 'update', course.course_uuid, db_session)
    if is_instructor:
        await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    else:
        await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
        if assignment_task_submission.user_id != current_user.id:
            raise HTTPException(status_code=403, detail='You can only update your own submissions')
        assignment_task_submission_object.grade = None
        assignment_task_submission_object.task_submission_grade_feedback = None
        assignment_task_submission_object.assignment_task_id = None
        assignment_task_submission_object.assignment_type = None
    for var, value in vars(assignment_task_submission_object).items():
        if value is not None:
            setattr(assignment_task_submission, var, value)
    assignment_task_submission.update_date = str(datetime.now())
    db_session.add(assignment_task_submission)
    await db_session.commit()
    await db_session.refresh(assignment_task_submission)
    return AssignmentTaskSubmissionRead.model_validate(assignment_task_submission)

async def delete_assignment_task_submission(request: Request, assignment_task_submission_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    _block_api_tokens(current_user)
    statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_submission_uuid == assignment_task_submission_uuid)
    assignment_task_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_task_submission:
        raise HTTPException(status_code=404, detail='Assignment Task Submission not found')
    statement = select(AssignmentTask).where(AssignmentTask.id == assignment_task_submission.assignment_task_id)
    assignment_task = (await db_session.execute(statement)).scalars().first()
    if not assignment_task:
        raise HTTPException(status_code=404, detail='Assignment Task not found')
    statement = select(Assignment).where(Assignment.id == assignment_task.assignment_id)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.DELETE)
    deleted_user_id = assignment_task_submission.user_id
    await db_session.delete(assignment_task_submission)
    await db_session.commit()
    if deleted_user_id is not None:
        graded_submission = (await db_session.execute(select(AssignmentUserSubmission).where(AssignmentUserSubmission.assignment_id == assignment.id, AssignmentUserSubmission.user_id == deleted_user_id, AssignmentUserSubmission.submission_status == AssignmentUserSubmissionStatus.GRADED))).scalars().first()
        if graded_submission is not None:
            try:
                await _apply_grade_and_finalize(assignment=assignment, course=course, user_id=deleted_user_id, assignment_user_submission=graded_submission, db_session=db_session, overall_feedback=None, auto_graded=True, dispatch_webhook=False)
                await _reconcile_certificate_after_grade_change(request, deleted_user_id, course, db_session)
            except Exception:
                logger.exception('Failed to recompute aggregate for user %s after deleting a task submission', deleted_user_id)
    return {'message': 'Assignment Task Submission deleted'}

async def create_assignment_submission(request: Request, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession, on_behalf_of_user_id: int | None=None):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    token_submitter = await _resolve_token_submission_user(request, db_session, current_user, course, on_behalf_of_user_id)
    if token_submitter is not None:
        submitter = token_submitter
        is_instructor = False
        is_token_submit = True
    else:
        _block_api_tokens(current_user)
        if isinstance(current_user, AnonymousUser):
            raise HTTPException(status_code=401, detail='Authentication required')
        submitter = current_user
        await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ, activity_id=assignment.activity_id)
        is_instructor = await authorization_verify_based_on_roles(request, current_user.id, 'update', course.course_uuid, db_session)
        is_token_submit = False
    if not is_instructor and (not is_token_submit) and _is_assignment_past_due(assignment):
        raise HTTPException(status_code=403, detail='Assignment deadline has passed')
    statement = select(AssignmentUserSubmission).where(AssignmentUserSubmission.assignment_id == assignment.id, AssignmentUserSubmission.user_id == submitter.id)
    assignment_user_submission = (await db_session.execute(statement)).scalars().first()
    if assignment_user_submission:
        reusable_states = (AssignmentUserSubmissionStatus.PENDING, AssignmentUserSubmissionStatus.NOT_SUBMITTED)
        if assignment_user_submission.submission_status not in reusable_states:
            raise HTTPException(status_code=400, detail='Assignment User Submission already exists')
    if assignment_user_submission:
        assignment_user_submission.submission_status = AssignmentUserSubmissionStatus.SUBMITTED
        assignment_user_submission.grade = 0
        assignment_user_submission.creation_date = str(datetime.now())
        assignment_user_submission.update_date = str(datetime.now())
    else:
        assignment_user_submission = AssignmentUserSubmission(user_id=submitter.id, assignment_id=assignment.id, grade=0, assignmentusersubmission_uuid=str(f'assignmentusersubmission_{uuid4()}'), submission_status=AssignmentUserSubmissionStatus.SUBMITTED, attempt_number=1, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db_session.add(assignment_user_submission)
    won_insert = True
    try:
        await db_session.commit()
    except IntegrityError:
        won_insert = False
        await db_session.rollback()
        assignment_user_submission = (await db_session.execute(select(AssignmentUserSubmission).where(AssignmentUserSubmission.assignment_id == assignment.id, AssignmentUserSubmission.user_id == submitter.id))).scalars().first()
        if assignment_user_submission is None:
            raise
        if assignment_user_submission.submission_status in (AssignmentUserSubmissionStatus.PENDING, AssignmentUserSubmissionStatus.NOT_SUBMITTED):
            assignment_user_submission.submission_status = AssignmentUserSubmissionStatus.SUBMITTED
            assignment_user_submission.update_date = str(datetime.now())
            db_session.add(assignment_user_submission)
            await db_session.commit()
    submitted_attempt_number = int(assignment_user_submission.attempt_number or 1)
    if won_insert:
        await track(event_name=analytics_events.ASSIGNMENT_SUBMITTED, org_id=course.org_id, user_id=submitter.id, properties={'assignment_uuid': assignment_uuid, 'course_uuid': course.course_uuid, 'attempt': submitted_attempt_number})
        await record_audit_event(event_type=UserAuditEventType.ASSIGNMENT_SUBMITTED, user_id=submitter.id, org_id=course.org_id, target_uuid=assignment_uuid, metadata={'course_uuid': course.course_uuid, 'course_name': course.name, 'attempt_number': submitted_attempt_number})
        await dispatch_webhooks(event_name=analytics_events.ASSIGNMENT_SUBMITTED, org_id=course.org_id, data={'user': {'user_uuid': submitter.user_uuid, 'email': submitter.email, 'username': submitter.username}, 'assignment': {'assignment_uuid': assignment_uuid}, 'course': {'course_uuid': course.course_uuid, 'name': course.name}, 'attempt_number': submitted_attempt_number})
    statement = select(User).where(User.id == submitter.id)
    user = (await db_session.execute(statement)).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail='User not found')
    statement = select(Activity).where(Activity.id == assignment.activity_id)
    activity = (await db_session.execute(statement)).scalars().first()
    if not activity:
        raise HTTPException(status_code=404, detail='Activity not found')
    trail = await check_trail_presence(org_id=course.org_id, user_id=user.id, request=request, user=user, db_session=db_session)
    statement = select(TrailRun).where(TrailRun.trail_id == trail.id, TrailRun.course_id == course.id, TrailRun.user_id == user.id)
    trailrun = (await db_session.execute(statement)).scalars().first()
    if not trailrun:
        trailrun = TrailRun(trail_id=trail.id if trail.id is not None else 0, course_id=course.id if course.id is not None else 0, org_id=course.org_id, user_id=user.id, creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(trailrun)
        await db_session.commit()
        await db_session.refresh(trailrun)
    statement = select(TrailStep).where(TrailStep.trailrun_id == trailrun.id, TrailStep.activity_id == activity.id, TrailStep.user_id == user.id)
    trailstep = (await db_session.execute(statement)).scalars().first()
    is_new_activity_completion = trailstep is None or not trailstep.complete
    if not trailstep:
        trailstep = TrailStep(trailrun_id=trailrun.id if trailrun.id is not None else 0, activity_id=activity.id if activity.id is not None else 0, course_id=course.id if course.id is not None else 0, trail_id=trail.id if trail.id is not None else 0, org_id=course.org_id, complete=True, teacher_verified=False, grade='', user_id=user.id, creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(trailstep)
        await db_session.commit()
        await db_session.refresh(trailstep)
    else:
        trailstep.complete = True
        trailstep.update_date = str(datetime.now())
        db_session.add(trailstep)
        await db_session.commit()
        await db_session.refresh(trailstep)
    if assignment.auto_grading and (not assignment.ungraded):
        tasks_statement = select(AssignmentTask).where(AssignmentTask.assignment_id == assignment.id)
        assignment_tasks = (await db_session.execute(tasks_statement)).scalars().all()
        all_auto_gradable = bool(assignment_tasks) and all((t.assignment_type in AUTO_GRADABLE_TASK_TYPES for t in assignment_tasks))
        if all_auto_gradable:
            await _apply_grade_and_finalize(assignment=assignment, course=course, user_id=int(submitter.id), assignment_user_submission=assignment_user_submission, db_session=db_session, overall_feedback=None, auto_graded=True, assignment_tasks=assignment_tasks)
            trailstep.complete = True
            trailstep.update_date = str(datetime.now())
            db_session.add(trailstep)
            await db_session.commit()
    if course and course.id and user and user.id:
        course_complete = await is_course_fully_completed(user.id, course.id, db_session)
        try:
            await check_course_completion_and_create_certificate(request, user.id, course.id, db_session, is_complete=course_complete)
        except Exception:
            logger.exception('Certificate check failed after assignment submission (assignment %s, user %s); submission is saved.', assignment.assignment_uuid, user.id)
        if is_new_activity_completion:
            try:
                if course_complete:
                    await track(event_name=analytics_events.COURSE_COMPLETED, org_id=course.org_id, user_id=user.id, properties={'course_uuid': course.course_uuid})
                    await dispatch_webhooks(event_name=analytics_events.COURSE_COMPLETED, org_id=course.org_id, data={'user': {'user_uuid': user.user_uuid, 'email': user.email, 'username': user.username}, 'course': {'course_uuid': course.course_uuid, 'name': course.name}})
            except Exception:
                logger.exception('COURSE_COMPLETED dispatch failed after assignment submission (assignment %s, user %s); submission is saved.', assignment.assignment_uuid, user.id)
    notify_ee_learner_progress(submitter.id, course.id)
    return AssignmentUserSubmissionRead.model_validate(assignment_user_submission)

async def read_assignment_submissions(request: Request, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession, limit: int=50, offset: int=0):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    is_instructor = await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)
    statement = select(AssignmentUserSubmission).where(AssignmentUserSubmission.assignment_id == assignment.id)
    if not is_instructor:
        statement = statement.where(AssignmentUserSubmission.user_id == current_user.id)
    statement = statement.order_by(AssignmentUserSubmission.id.desc()).limit(limit).offset(offset)
    tasks_statement = select(AssignmentTask).where(AssignmentTask.assignment_id == assignment.id)
    assignment_tasks = (await db_session.execute(tasks_statement)).scalars().all()
    max_grade = sum((int(t.max_grade_value or 0) for t in assignment_tasks))
    submissions = (await db_session.execute(statement)).scalars().all()
    task_ids = [t.id for t in assignment_tasks if t.id is not None]
    user_ids = [s.user_id for s in submissions if s.user_id is not None]
    submissions_by_user_task: dict = {}
    if task_ids and user_ids:
        task_sub_rows = (await db_session.execute(select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.assignment_task_id.in_(task_ids), AssignmentTaskSubmission.user_id.in_(user_ids)))).scalars().all()
        for ts in task_sub_rows:
            submissions_by_user_task.setdefault(ts.user_id, {})[ts.assignment_task_id] = ts
    results = []
    for sub in submissions:
        row = AssignmentUserSubmissionRead.model_validate(sub).model_dump()
        if sub.submission_status == AssignmentUserSubmissionStatus.GRADED:
            grade_display = compute_assignment_grade(int(sub.grade or 0), max_grade, assignment.grading_type, pass_threshold_percentage=assignment.pass_threshold_percentage)
            grade_display['tasks'] = _build_tasks_breakdown(assignment_tasks, submissions_by_user_task.get(sub.user_id, {}), grade_display['passing_threshold'])
            row['grade_display'] = grade_display
        else:
            row['grade_display'] = None
        results.append(row)
    return results

async def read_user_assignment_submissions(request: Request, assignment_uuid: str, user_id: int, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    is_instructor = await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)
    if not is_instructor and int(user_id) != int(current_user.id):
        raise HTTPException(status_code=403, detail='You can only view your own submissions')
    statement = select(AssignmentUserSubmission).where(AssignmentUserSubmission.assignment_id == assignment.id, AssignmentUserSubmission.user_id == user_id)
    return [AssignmentUserSubmissionRead.model_validate(assignment_user_submission) for assignment_user_submission in (await db_session.execute(statement)).scalars().all()]

async def read_user_assignment_submissions_me(request: Request, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    _block_api_tokens(current_user)
    return await read_user_assignment_submissions(request, assignment_uuid, current_user.id, current_user, db_session)

async def update_assignment_submission(request: Request, user_id: int, assignment_uuid: str, assignment_user_submission_object: AssignmentUserSubmissionCreate, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == user_id, AssignmentUserSubmission.assignment_id == assignment.id)
    assignment_user_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_user_submission:
        raise HTTPException(status_code=404, detail='Assignment User Submission not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    is_instructor = await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)
    if is_instructor:
        await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    else:
        await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
        if str(assignment_user_submission.user_id) != str(current_user.id):
            raise HTTPException(status_code=403, detail='You can only update your own submissions')
        for protected_field in ('grade', 'submission_status'):
            if hasattr(assignment_user_submission_object, protected_field):
                setattr(assignment_user_submission_object, protected_field, None)
    for identity_field in ('user_id', 'assignment_id'):
        if hasattr(assignment_user_submission_object, identity_field):
            setattr(assignment_user_submission_object, identity_field, None)
    for var, value in vars(assignment_user_submission_object).items():
        if value is not None:
            setattr(assignment_user_submission, var, value)
    assignment_user_submission.update_date = str(datetime.now())
    db_session.add(assignment_user_submission)
    await db_session.commit()
    await db_session.refresh(assignment_user_submission)
    notify_ee_learner_progress(assignment_user_submission.user_id, course.id)
    return AssignmentUserSubmissionRead.model_validate(assignment_user_submission)

async def delete_assignment_submission(request: Request, user_id: int, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    _block_api_tokens(current_user)
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == user_id, AssignmentUserSubmission.assignment_id == assignment.id)
    assignment_user_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_user_submission:
        raise HTTPException(status_code=404, detail='Assignment User Submission not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.DELETE)
    trailstep_statement = select(TrailStep).where(TrailStep.activity_id == assignment.activity_id, TrailStep.user_id == user_id)
    trailstep = (await db_session.execute(trailstep_statement)).scalars().first()
    if trailstep:
        trailstep.complete = False
        trailstep.teacher_verified = False
        trailstep.grade = ''
        trailstep.update_date = str(datetime.now())
        db_session.add(trailstep)
    await db_session.delete(assignment_user_submission)
    await db_session.commit()
    if course.id:
        await revoke_user_certificate(user_id, course.id, db_session, reason='assignment_rejected')
        await sync_trailrun_status(user_id, course.id, db_session)
        notify_ee_learner_progress(user_id, course.id)
    return {'message': 'Assignment User Submission deleted'}

async def retry_assignment_submission(request: Request, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    """
    Reset the current user's submission for ``assignment_uuid`` so they can
    attempt it again. The teacher must have opted in via ``allow_retries``;
    we additionally bound the number of retries with ``max_retries`` (0
    means unlimited).

    On retry we keep the AssignmentUserSubmission row in place so the
    attempt counter survives. Everything else is wiped: per-task
    submissions are deleted, the row is flipped to PENDING with grade=0,
    the matching TrailStep is reset to incomplete, and any issued course
    certificate is revoked. The student then re-fills the tasks and
    submits again, at which point ``create_assignment_submission``
    transitions the existing PENDING row to SUBMITTED.
    """
    _block_api_tokens(current_user)
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    if not assignment.allow_retries:
        raise HTTPException(status_code=403, detail='Retries are not enabled for this assignment')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    statement = select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == current_user.id, AssignmentUserSubmission.assignment_id == assignment.id).with_for_update()
    assignment_user_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_user_submission:
        raise HTTPException(status_code=404, detail='Assignment User Submission not found')
    retryable_statuses = _HANDED_IN_STATUSES if assignment.ungraded else (AssignmentUserSubmissionStatus.GRADED,)
    if assignment_user_submission.submission_status not in retryable_statuses:
        raise HTTPException(status_code=400, detail='Only submitted assignments can be retried' if assignment.ungraded else 'Only graded submissions can be retried')
    if _is_assignment_past_due(assignment) and (not await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)):
        raise HTTPException(status_code=403, detail='Assignment deadline has passed')
    current_attempt = int(assignment_user_submission.attempt_number or 1)
    max_attempts = int(assignment.max_retries or 0)
    if max_attempts and current_attempt >= max_attempts:
        raise HTTPException(status_code=403, detail='No retry attempts remaining')
    task_ids_statement = select(AssignmentTask.id).where(AssignmentTask.assignment_id == assignment.id)
    task_ids = [tid for tid in (await db_session.execute(task_ids_statement)).scalars().all() if tid is not None]
    if task_ids:
        existing_submissions = (await db_session.execute(select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.user_id == current_user.id, AssignmentTaskSubmission.assignment_task_id.in_(task_ids)))).scalars().all()
        for ts in existing_submissions:
            await db_session.delete(ts)
    trailstep_statement = select(TrailStep).where(TrailStep.activity_id == assignment.activity_id, TrailStep.user_id == int(current_user.id))
    trailstep = (await db_session.execute(trailstep_statement)).scalars().first()
    if trailstep:
        trailstep.complete = False
        trailstep.teacher_verified = False
        trailstep.grade = ''
        trailstep.update_date = str(datetime.now())
        db_session.add(trailstep)
    assignment_user_submission.submission_status = AssignmentUserSubmissionStatus.PENDING
    assignment_user_submission.grade = 0
    assignment_user_submission.overall_feedback = None
    assignment_user_submission.attempt_number = current_attempt + 1
    assignment_user_submission.update_date = str(datetime.now())
    db_session.add(assignment_user_submission)
    await db_session.commit()
    await db_session.refresh(assignment_user_submission)
    if course.id:
        await revoke_user_certificate(int(current_user.id), course.id, db_session, reason='assignment_retried')
    if course.id:
        await sync_trailrun_status(int(current_user.id), course.id, db_session)
        notify_ee_learner_progress(int(current_user.id), course.id)
    return {'message': 'Assignment User Submission reset for retry', 'attempt_number': assignment_user_submission.attempt_number, 'max_retries': max_attempts, 'submission': AssignmentUserSubmissionRead.model_validate(assignment_user_submission).model_dump()}

async def _apply_grade_and_finalize(assignment: Assignment, course: Course, user_id: int, assignment_user_submission: AssignmentUserSubmission, db_session: AsyncSession, overall_feedback: str | None=None, auto_graded: bool=False, dispatch_webhook: bool=True, assignment_tasks: Sequence[AssignmentTask] | None=None) -> dict:
    """
    Core grading logic shared by manual and auto-grade flows. Computes the
    final grade from existing per-task submissions, persists it with status
    GRADED, dispatches the webhook, and returns the enriched grade dict
    (including per-task breakdown).

    ``dispatch_webhook=False`` is for bulk recomputation (e.g. after a teacher
    deletes a task) where the grade is being corrected rather than newly
    awarded. Firing ``assignment_graded`` once per enrolled learner on an
    admin edit would be a webhook storm, and integrations would read it as a
    fresh grading event.

    IMPORTANT: This helper does NO permission checks. Callers must enforce
    access control before calling it. It exists so that both the teacher's
    manual grading endpoint (UPDATE permission) and the student's auto-grade
    path (READ permission, self-grading under teacher-configured auto_grading)
    can share one implementation.

    Refuses to run on a formative (``ungraded``) assignment. The guard lives
    here rather than only in the grading endpoint so every route into grading
    (manual, auto, and the bulk regrade after a task edit) is covered by one
    check, and a formative submission can never be flipped to GRADED.
    """
    if assignment.ungraded:
        raise HTTPException(status_code=400, detail='This assignment is ungraded (formative) and cannot be graded.')
    if assignment_tasks is None:
        tasks_statement = select(AssignmentTask).where(AssignmentTask.assignment_id == assignment.id)
        assignment_tasks = (await db_session.execute(tasks_statement)).scalars().all()
    max_grade = 0
    for task in assignment_tasks:
        max_grade += int(task.max_grade_value or 0)
    task_ids = [task.id for task in assignment_tasks if task.id is not None]
    task_submissions_by_task_id: dict = {}
    if task_ids:
        ts_statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.user_id == user_id, AssignmentTaskSubmission.assignment_task_id.in_(task_ids))
        for ts in (await db_session.execute(ts_statement)).scalars().all():
            task_submissions_by_task_id[ts.assignment_task_id] = ts
    code_unverifiable = False
    for task in assignment_tasks:
        ts = task_submissions_by_task_id.get(task.id)
        if ts is not None and ts.manually_graded:
            continue
        verified = await _server_verified_task_grade(task, ts)
        if verified is None and ts is not None and (task.assignment_type == AssignmentTaskTypeEnum.CODE):
            code_unverifiable = True
        if verified is None or ts is None:
            continue
        if int(ts.grade or 0) != verified:
            ts.grade = verified
            ts.task_submission_grade_feedback = 'Server-verified: correct' if verified > 0 else 'Server-verified: incorrect'
            db_session.add(ts)
    if auto_graded and code_unverifiable:
        db_session.add(assignment_user_submission)
        await db_session.commit()
        await db_session.refresh(assignment_user_submission)
        logger.warning('Auto-grade deferred for assignment %s user %s: a CODE task could not be server-verified; submission left SUBMITTED (pending grade).', assignment.assignment_uuid, user_id)
        return {'finalized': False, 'reason': 'code_unverifiable'}
    raw_grade = 0
    for ts in task_submissions_by_task_id.values():
        raw_grade += int(ts.grade or 0)
    if overall_feedback is not None:
        assignment_user_submission.overall_feedback = overall_feedback or None
    computed = compute_assignment_grade(raw_grade, max_grade, assignment.grading_type, overall_feedback=assignment_user_submission.overall_feedback, pass_threshold_percentage=assignment.pass_threshold_percentage)
    computed['tasks'] = _build_tasks_breakdown(assignment_tasks, task_submissions_by_task_id, computed['passing_threshold'])
    assignment_user_submission.grade = computed['grade']
    assignment_user_submission.submission_status = AssignmentUserSubmissionStatus.GRADED
    db_session.add(assignment_user_submission)
    await db_session.commit()
    await db_session.refresh(assignment_user_submission)
    await record_audit_event(event_type=UserAuditEventType.ASSIGNMENT_GRADED, user_id=user_id, org_id=course.org_id, target_uuid=assignment.assignment_uuid, metadata={'course_uuid': course.course_uuid, 'course_name': course.name, 'grade': computed['grade'], 'max_grade': computed['max_grade'], 'percentage': computed['percentage'], 'display_grade': computed['display_grade'], 'letter_grade': computed['letter_grade'], 'passed': computed['passed'], 'attempt_number': int(assignment_user_submission.attempt_number or 1), 'auto_graded': auto_graded})
    notify_ee_learner_progress(user_id, course.id)
    if dispatch_webhook:
        await track(event_name=analytics_events.ASSIGNMENT_GRADED, org_id=course.org_id, user_id=user_id, properties={'course_uuid': course.course_uuid, 'assignment_uuid': assignment.assignment_uuid, 'grade': computed['grade'], 'max_grade': computed['max_grade'], 'passed': computed['passed']})
        for row in computed['tasks']:
            if row['assignment_type'] == AssignmentTaskTypeEnum.QUIZ and row['submitted']:
                await track(event_name=analytics_events.QUIZ_SUBMITTED, org_id=course.org_id, user_id=user_id, properties={'course_uuid': course.course_uuid, 'score': row['grade'], 'max_score': row['max_grade'], 'passed': row['passed']})
        await dispatch_webhooks(event_name='assignment_graded', org_id=course.org_id, data={'user_id': user_id, 'assignment_uuid': assignment.assignment_uuid, 'course_uuid': course.course_uuid, 'grade': computed['grade'], 'max_grade': computed['max_grade'], 'percentage': computed['percentage'], 'display_grade': computed['display_grade'], 'letter_grade': computed['letter_grade'], 'points_summary': computed['points_summary'], 'passed': computed['passed'], 'grading_type': computed['grading_type'], 'overall_feedback': computed['overall_feedback'], 'auto_graded': auto_graded})
    return computed

async def grade_assignment_submission(request: Request, user_id: int, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession, overall_feedback: str | None=None):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    statement = select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == user_id, AssignmentUserSubmission.assignment_id == assignment.id)
    assignment_user_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_user_submission:
        raise HTTPException(status_code=404, detail='Assignment User Submission not found')
    if assignment_user_submission.submission_status in (AssignmentUserSubmissionStatus.PENDING, AssignmentUserSubmissionStatus.NOT_SUBMITTED):
        raise HTTPException(status_code=400, detail='This learner has not handed in an attempt yet; nothing to grade.')
    computed = await _apply_grade_and_finalize(assignment=assignment, course=course, user_id=user_id, assignment_user_submission=assignment_user_submission, db_session=db_session, overall_feedback=overall_feedback, auto_graded=False)
    if course.id:
        try:
            await check_course_completion_and_create_certificate(request, user_id, course.id, db_session)
            if not await are_course_assignments_passed(user_id, course.id, db_session):
                await revoke_user_certificate(user_id, course.id, db_session, reason='regraded_below_threshold')
        except Exception:
            pass
    return {'message': f"Assignment User Submission graded: {computed['display_grade']}", **computed}

async def get_grade_assignment_submission(request: Request, user_id: int, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    if assignment.ungraded:
        raise HTTPException(status_code=400, detail='This assignment is ungraded (formative) and has no grade.')
    is_instructor = await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)
    if not is_instructor and str(user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail='You can only view your own grade')
    statement = select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == user_id, AssignmentUserSubmission.assignment_id == assignment.id)
    assignment_user_submission = (await db_session.execute(statement)).scalars().first()
    if not assignment_user_submission:
        raise HTTPException(status_code=404, detail='Assignment User Submission not found')
    tasks_statement = select(AssignmentTask).where(AssignmentTask.assignment_id == assignment.id)
    assignment_tasks = (await db_session.execute(tasks_statement)).scalars().all()
    max_grade = 0
    for task in assignment_tasks:
        max_grade += int(task.max_grade_value or 0)
    task_ids = [task.id for task in assignment_tasks if task.id is not None]
    task_submissions_by_task_id: dict = {}
    if task_ids:
        ts_statement = select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.user_id == user_id, AssignmentTaskSubmission.assignment_task_id.in_(task_ids))
        for ts in (await db_session.execute(ts_statement)).scalars().all():
            task_submissions_by_task_id[ts.assignment_task_id] = ts
    grade_obj = compute_assignment_grade(int(assignment_user_submission.grade or 0), max_grade, assignment.grading_type, overall_feedback=assignment_user_submission.overall_feedback, pass_threshold_percentage=assignment.pass_threshold_percentage)
    grade_obj['tasks'] = _build_tasks_breakdown(assignment_tasks, task_submissions_by_task_id, grade_obj['passing_threshold'])
    return grade_obj

async def mark_activity_as_done_for_user(request: Request, user_id: int, assignment_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    _block_api_tokens(current_user)
    statement = select(Assignment).where(Assignment.assignment_uuid == assignment_uuid)
    assignment = (await db_session.execute(statement)).scalars().first()
    if not assignment:
        raise HTTPException(status_code=404, detail='Assignment not found')
    statement = select(Activity).where(Activity.id == assignment.activity_id)
    activity = (await db_session.execute(statement)).scalars().first()
    statement = select(Course).where(Course.id == assignment.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    if not activity:
        raise HTTPException(status_code=404, detail='Activity not found')
    statement = select(User).where(User.id == user_id)
    user = (await db_session.execute(statement)).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail='User not found')
    trailsteps = select(TrailStep).where(TrailStep.activity_id == activity.id, TrailStep.user_id == user_id)
    trailstep = (await db_session.execute(trailsteps)).scalars().first()
    if not trailstep:
        raise HTTPException(status_code=404, detail='User not enrolled in the course')
    trailstep.complete = True
    trailstep.update_date = str(datetime.now())
    db_session.add(trailstep)
    await db_session.commit()
    await db_session.refresh(trailstep)
    if course and course.id:
        await check_course_completion_and_create_certificate(request, user_id, course.id, db_session)
        notify_ee_learner_progress(user_id, course.id)
    return {'message': 'Activity marked as done for user'}

async def get_assignments_from_course(request: Request, course_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    statement = select(Course).where(Course.course_uuid == course_uuid)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await authorize_assignment_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    statement = select(Assignment).where(Assignment.course_id == course.id)
    is_instructor = await _is_assignment_instructor(request, current_user, course.course_uuid, db_session)
    if not is_instructor:
        statement = statement.where(Assignment.published == True)
    assignments = (await db_session.execute(statement)).scalars().all()
    if not is_instructor:
        activities = {a.id: a for a in (await db_session.execute(select(Activity).where(Activity.id.in_({x.activity_id for x in assignments})))).scalars().all()} if assignments else {}
        readable = []
        for assignment in assignments:
            activity = activities.get(assignment.activity_id)
            if activity is None:
                continue
            try:
                await verify_activity_reader_access(request, activity, course, current_user, db_session)
            except HTTPException:
                continue
            readable.append(assignment)
        assignments = readable
    results = []
    for assignment in assignments:
        has_solution = bool((assignment.solution or '').strip() or assignment.solution_file)
        unlocked = has_solution and (is_instructor or await _student_may_see_solution(current_user, assignment, db_session))
        results.append(_apply_solution_visibility(AssignmentRead.model_validate(assignment), assignment, unlocked=unlocked))
    return results
