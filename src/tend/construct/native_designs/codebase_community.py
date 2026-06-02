from __future__ import annotations

from typing import Any

from ..native_recipe import NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__


def build_native_recipe(source: Any, db_id: str) -> NativeMigrationRecipe:
    source.schema(db_id)
    return recipe(
        db_id,
        version=DESIGN_VERSION,
        design_goal=(
            "Represent StackExchange-style community posts with vote histograms, "
            "revision timelines, and typed user/post/tag entities."
        ),
        collections=[
            collection(
                "community_posts",
                purpose="Post documents with dynamic vote-type buckets.",
                source_tables=["posts", "votes", "comments", "postHistory"],
                transforms=[
                    transform(
                        "votes_by_type",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="posts",
                        child_table="votes",
                        join=join("posts.Id", "votes.PostId"),
                        target_field="votes_by_type",
                        key=expr("votes.VoteTypeId", "votes.VoteTypeId"),
                        values={
                            "vote_count": expr("count(votes.Id)", "votes.Id"),
                            "bounty_total": expr(
                                "sum(votes.BountyAmount)",
                                "votes.BountyAmount",
                            ),
                        },
                    ),
                    transform(
                        "post_revision_events",
                        "nested_event_stream",
                        module_ref=MODULE_REF,
                        parent_table="posts",
                        event_source_table="postHistory",
                        join=join("posts.Id", "postHistory.PostId"),
                        target_field="revision_events",
                        event_type_field="postHistory.PostHistoryTypeId",
                        event_time_field="postHistory.CreationDate",
                        event_payload={
                            "user_id": "postHistory.UserId",
                            "text": "postHistory.Text",
                            "comment": "postHistory.Comment",
                            "revision_guid": "postHistory.RevisionGUID",
                        },
                    ),
                    transform(
                        "post_state_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="post_tags",
                        tags={
                            "question": {
                                "condition": "posts.PostTypeId == 1",
                                "provenance": ["posts.PostTypeId"],
                            },
                            "closed": {
                                "condition": "posts.ClosedDate is not null",
                                "provenance": ["posts.ClosedDate"],
                            },
                            "high_score": {
                                "condition": "posts.Score >= 10",
                                "provenance": ["posts.Score"],
                            },
                        },
                    ),
                ],
            ),
            collection(
                "community_entities",
                purpose="Typed users, posts, and tags for entity-centric workloads.",
                source_tables=["users", "posts", "tags"],
                transforms=[
                    transform(
                        "community_entity_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="entity_type",
                        variants={
                            "user": {
                                "source_table": "users",
                                "fields": {
                                    "entity_id": expr("concat('user:', users.Id)", "users.Id"),
                                    "display_name": field_source("users.DisplayName"),
                                    "reputation": field_source("users.Reputation"),
                                },
                            },
                            "post": {
                                "source_table": "posts",
                                "fields": {
                                    "entity_id": expr("concat('post:', posts.Id)", "posts.Id"),
                                    "title": field_source("posts.Title"),
                                    "score": field_source("posts.Score"),
                                },
                            },
                            "tag": {
                                "source_table": "tags",
                                "fields": {
                                    "entity_id": expr("concat('tag:', tags.Id)", "tags.Id"),
                                    "tag_name": field_source("tags.TagName"),
                                    "count": field_source("tags.Count"),
                                },
                            },
                        },
                    )
                ],
            ),
        ],
    )
