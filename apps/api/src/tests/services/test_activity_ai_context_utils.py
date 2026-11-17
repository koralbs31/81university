"""Tests for src/services/courses/activities/utils.py.

These helpers flatten an activity's rich-text document into the plain-text
context handed to the AI assistant: headings, callouts and paragraphs are
pulled out of the editor JSON, then serialized into a single prompt string.
"""
from types import SimpleNamespace
from src.services.courses.activities.utils import structure_activity_content_by_type

def _text(t):
    return {'type': 'text', 'text': t}

class TestStructureActivityContentByType:

    def test_missing_or_empty_content_returns_empty_list(self):
        assert structure_activity_content_by_type({}) == []
        assert structure_activity_content_by_type({'content': []}) == []
        assert structure_activity_content_by_type({'content': None}) == []

    def test_extracts_headings_callouts_and_paragraphs(self):
        activity = {'content': [{'type': 'heading', 'content': [_text('Intro')]}, {'type': 'paragraph', 'content': [_text('Hello world')]}, {'type': 'calloutInfo', 'content': [_text('Note: '), _text('be careful')]}, {'type': 'calloutWarning', 'content': [_text('Danger')]}, {'type': 'heading', 'content': [_text('Outro')]}]}
        assert structure_activity_content_by_type(activity) == [{'Headings': ['Intro', 'Outro']}, {'Callouts': ['Note: be careful', 'Danger']}, {'Paragraphs': ['Hello world']}]

    def test_skips_nodes_without_text(self):
        """Nodes with no content, non-text children, or unknown types are ignored."""
        activity = {'content': [{'type': 'paragraph'}, {'type': 'heading', 'content': [{'type': 'image'}]}, {'type': 'paragraph', 'content': [{'type': 'hardBreak'}]}, {'type': 'calloutInfo', 'content': [_text('a'), {'type': 'image'}]}, {'type': 'codeBlock', 'content': [_text('print(1)')]}]}
        assert structure_activity_content_by_type(activity) == [{'Headings': []}, {'Callouts': []}, {'Paragraphs': []}]

class TestSerializeActivityText:
    course = SimpleNamespace(name='Python 101')
    activity = SimpleNamespace(name='Loops')
