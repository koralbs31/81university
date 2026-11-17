"""
RBAC Resource Configurations

This module defines configurations for each resource type that the RBAC system handles.
Adding a new resource type is as simple as adding a new entry to RESOURCE_CONFIGS.
"""
from src.security.rbac.types import ResourceConfig
RESOURCE_CONFIGS: dict[str, ResourceConfig] = {'courses': ResourceConfig(resource_type='courses', uuid_prefix='course_', has_published_field=True, supports_usergroups=True, supports_authorship=True, model_name='Course', uuid_field='course_uuid'), 'communities': ResourceConfig(resource_type='communities', uuid_prefix='community_', has_published_field=False, supports_usergroups=True, supports_authorship=False, model_name='Community', uuid_field='community_uuid'), 'coursechapters': ResourceConfig(resource_type='coursechapters', uuid_prefix='chapter_', has_published_field=False, supports_usergroups=False, supports_authorship=False, model_name='Chapter', uuid_field='chapter_uuid', parent_resource_type='courses', parent_id_field='course_id'), 'activities': ResourceConfig(resource_type='activities', uuid_prefix='activity_', has_published_field=False, supports_usergroups=False, supports_authorship=False, model_name='Activity', uuid_field='activity_uuid', parent_resource_type='coursechapters', parent_id_field='chapter_id'), 'discussions': ResourceConfig(resource_type='discussions', uuid_prefix='discussion_', has_published_field=False, supports_usergroups=False, supports_authorship=False, model_name='Discussion', uuid_field='discussion_uuid', parent_resource_type='communities', parent_id_field='community_id')}

def get_resource_config(resource_uuid: str) -> ResourceConfig | None:
    """
    Get the resource configuration based on the UUID prefix.

    Args:
        resource_uuid: The UUID of the resource

    Returns:
        ResourceConfig if found, None otherwise
    """
    if not resource_uuid:
        return None
    for config in RESOURCE_CONFIGS.values():
        if resource_uuid.startswith(config.uuid_prefix):
            return config
    return None

def get_resource_type(resource_uuid: str) -> str | None:
    """
    Get the resource type from a UUID.

    Args:
        resource_uuid: The UUID of the resource

    Returns:
        Resource type string if found, None otherwise
    """
    config = get_resource_config(resource_uuid)
    return config.resource_type if config else None
