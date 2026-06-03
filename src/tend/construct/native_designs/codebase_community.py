from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..native_audit import audit_database_structure, validate_structure_gate
from ..native_executor import NativeExecutionResult
from ..native_recipe import NativeFeature, NativeFeatureManifest, NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__
_TAG_RE = re.compile(r"<([^>]+)>")


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


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> NativeExecutionResult:
    """Build community thread, reputation, and tag-ecosystem documents from BIRD rows."""
    if db_id != "codebase_community":
        raise ValueError(f"codebase_community materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    posts = _rows(
        conn,
        "posts",
        ["Id"],
        [
            "Id",
            "PostTypeId",
            "AcceptedAnswerId",
            "CreaionDate",
            "Score",
            "ViewCount",
            "OwnerUserId",
            "LasActivityDate",
            "Title",
            "Tags",
            "AnswerCount",
            "CommentCount",
            "FavoriteCount",
            "LastEditorUserId",
            "LastEditDate",
            "CommunityOwnedDate",
            "ParentId",
            "ClosedDate",
            "OwnerDisplayName",
            "LastEditorDisplayName",
        ],
    )
    users = _rows(
        conn,
        "users",
        ["Id"],
        [
            "Id",
            "Reputation",
            "CreationDate",
            "DisplayName",
            "LastAccessDate",
            "Location",
            "Views",
            "UpVotes",
            "DownVotes",
            "AccountId",
            "Age",
        ],
    )
    tags = _rows(conn, "tags", ["TagName"], ["Id", "TagName", "Count", "ExcerptPostId", "WikiPostId"])

    users_by_id = _by_id(users, "Id")
    tags_by_name = {str(row.get("TagName")): row for row in tags if row.get("TagName")}
    posts_by_id = _by_id(posts, "Id")
    answers_by_parent = _group(
        [post for post in posts if post.get("PostTypeId") == 2 and post.get("ParentId") is not None],
        "ParentId",
    )
    question_posts = [post for post in posts if post.get("PostTypeId") == 1]

    vote_buckets = _vote_buckets(conn)
    comment_years = _comment_year_buckets(conn)
    comment_samples = _comment_samples(conn)
    history_years = _history_year_buckets(conn)
    link_buckets = _post_link_buckets(conn)
    user_activity = _user_activity_lattice(conn)
    user_badges = _user_badge_buckets(conn)
    tag_thread_index = _tag_thread_index(question_posts)

    thread_docs = [
        _community_thread_doc(
            question,
            users_by_id=users_by_id,
            tags_by_name=tags_by_name,
            answers=answers_by_parent.get(question.get("Id"), []),
            posts_by_id=posts_by_id,
            vote_buckets=vote_buckets,
            comment_years=comment_years,
            comment_samples=comment_samples,
            history_years=history_years,
            link_buckets=link_buckets,
        )
        for question in question_posts
    ]
    user_docs = [
        _user_reputation_profile(
            user,
            activity=user_activity.get(user.get("Id"), {}),
            badge_buckets=user_badges.get(user.get("Id"), {}),
        )
        for user in users
    ]
    tag_docs = [
        _tag_topic_ecosystem(
            tag,
            thread_refs=tag_thread_index.get(str(tag.get("TagName")), []),
            posts_by_id=posts_by_id,
        )
        for tag in tags
    ]
    data = {
        "community_threads": thread_docs,
        "tag_topic_ecosystems": tag_docs,
        "user_reputation_profiles": user_docs,
    }

    audit = audit_database_structure(db_id, data)
    manifest = _manifest()
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "community_threads": {
                "document_count": len(thread_docs),
                "root_entity": "question thread with answers, comments, tags, votes, revisions",
                "source_tables": [
                    "posts",
                    "comments",
                    "votes",
                    "postHistory",
                    "postLinks",
                    "tags",
                    "users",
                ],
            },
            "user_reputation_profiles": {
                "document_count": len(user_docs),
                "root_entity": "community user reputation and participation profile",
                "source_tables": ["users", "badges", "posts", "comments", "votes"],
            },
            "tag_topic_ecosystems": {
                "document_count": len(tag_docs),
                "root_entity": "tag/topic ecosystem",
                "source_tables": ["tags", "posts", "postLinks"],
            },
        },
        "structure_audit": audit.to_dict(),
        "structure_gate": validate_structure_gate(audit).to_dict(),
    }
    provenance = {
        "db_id": db_id,
        "conversion_code_ref": f"{MODULE_REF}.materialize_native_dataworld",
        "entries": {
            feature.id: {
                "source_tables": _source_tables_from_refs(feature.provenance_refs),
                "provenance_refs": list(feature.provenance_refs),
                "field": feature.field,
            }
            for feature in manifest.features
        },
    }
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "codebase_community_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(manifest.features),
            max_depth=audit.max_depth,
            gate_ok=validate_structure_gate(audit).ok,
            world_signature=signature,
        )
    return NativeExecutionResult(
        data=data,
        schema=native_schema,
        manifest=manifest,
        provenance=provenance,
        world_signature=signature,
        validation=None,
    )


def _rows(
    conn: Any,
    table: str,
    order_by: list[str],
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    select_sql = "*" if columns is None else ", ".join(f'"{name}"' for name in columns)
    order_sql = ", ".join(f'"{name}"' for name in order_by)
    cursor = conn.execute(f'SELECT {select_sql} FROM "{table}" ORDER BY {order_sql}')
    names = [str(item[0]) for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _query_rows(conn: Any, sql: str) -> list[dict[str, Any]]:
    cursor = conn.execute(sql)
    names = [str(item[0]) for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _by_id(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row.get(key): row for row in rows if row.get(key) is not None}


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    out: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row.get(key)].append(row)
    return dict(out)


def _vote_buckets(conn: Any) -> dict[Any, dict[str, dict[str, Any]]]:
    rows = _query_rows(
        conn,
        """
        SELECT
          PostId,
          VoteTypeId,
          count(*) AS vote_count,
          sum(coalesce(BountyAmount, 0)) AS bounty_total,
          min(CreationDate) AS first_vote_date,
          max(CreationDate) AS last_vote_date,
          count(UserId) AS known_voter_count
        FROM votes
        GROUP BY PostId, VoteTypeId
        ORDER BY PostId, VoteTypeId
        """,
    )
    out: dict[Any, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        key = str(row.get("VoteTypeId"))
        out[row.get("PostId")][key] = {
            "vote_type_id": row.get("VoteTypeId"),
            "count": row.get("vote_count") or 0,
            "bounty_total": row.get("bounty_total") or 0,
            "first_vote_date": row.get("first_vote_date"),
            "last_vote_date": row.get("last_vote_date"),
            "known_voter_state": "present" if row.get("known_voter_count") else "missing",
            "evidence": {
                "source": {
                    "table": "votes",
                    "columns": ["PostId", "VoteTypeId", "BountyAmount", "UserId"],
                }
            },
        }
    return dict(out)


def _comment_year_buckets(conn: Any) -> dict[Any, dict[str, dict[str, Any]]]:
    rows = _query_rows(
        conn,
        """
        SELECT
          PostId,
          substr(CreationDate, 1, 4) AS comment_year,
          count(*) AS comment_count,
          sum(CASE WHEN Score > 0 THEN 1 ELSE 0 END) AS positive_count,
          max(Score) AS max_score
        FROM comments
        GROUP BY PostId, substr(CreationDate, 1, 4)
        ORDER BY PostId, comment_year
        """,
    )
    out: dict[Any, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        year = str(row.get("comment_year") or "unknown")
        out[row.get("PostId")][year] = {
            "year": year,
            "count": row.get("comment_count") or 0,
            "positive_count": row.get("positive_count") or 0,
            "max_score": row.get("max_score") or 0,
        }
    return dict(out)


def _comment_samples(conn: Any) -> dict[Any, list[dict[str, Any]]]:
    rows = _query_rows(
        conn,
        """
        SELECT Id, PostId, Score, CreationDate, UserId, UserDisplayName
        FROM comments
        ORDER BY PostId, Score DESC, CreationDate ASC, Id ASC
        """,
    )
    out: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if len(out[row.get("PostId")]) >= 3:
            continue
        out[row.get("PostId")].append(
            {
                "comment_id": row.get("Id"),
                "score": row.get("Score") or 0,
                "created_at": row.get("CreationDate"),
                "user_id": row.get("UserId"),
                "display_name": row.get("UserDisplayName"),
                "author_state": "present" if row.get("UserId") else "missing",
            }
        )
    return dict(out)


def _history_year_buckets(conn: Any) -> dict[Any, dict[str, dict[str, Any]]]:
    rows = _query_rows(
        conn,
        """
        SELECT
          PostId,
          substr(CreationDate, 1, 4) AS history_year,
          PostHistoryTypeId,
          count(*) AS event_count,
          count(UserId) AS known_editor_count,
          max(CreationDate) AS latest_event_at
        FROM postHistory
        GROUP BY PostId, substr(CreationDate, 1, 4), PostHistoryTypeId
        ORDER BY PostId, history_year, PostHistoryTypeId
        """,
    )
    out: dict[Any, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        year = str(row.get("history_year") or "unknown")
        year_bucket = out[row.get("PostId")].setdefault(
            year,
            {
                "year": year,
                "events": [],
                "editor_presence_state": "missing",
            },
        )
        if row.get("known_editor_count"):
            year_bucket["editor_presence_state"] = "present"
        year_bucket["events"].append(
            {
                "history_type_id": row.get("PostHistoryTypeId"),
                "count": row.get("event_count") or 0,
                "latest_event_at": row.get("latest_event_at"),
            }
        )
    return dict(out)


def _post_link_buckets(conn: Any) -> dict[Any, dict[str, list[dict[str, Any]]]]:
    rows = _query_rows(
        conn,
        """
        SELECT PostId, RelatedPostId, LinkTypeId, CreationDate
        FROM postLinks
        ORDER BY PostId, LinkTypeId, CreationDate, Id
        """,
    )
    out: dict[Any, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        link_type = str(row.get("LinkTypeId") or "unknown")
        out[row.get("PostId")][link_type].append(
            {
                "related_post_id": row.get("RelatedPostId"),
                "created_at": row.get("CreationDate"),
            }
        )
    return {post_id: dict(bucket) for post_id, bucket in out.items()}


def _user_activity_lattice(conn: Any) -> dict[Any, dict[str, Any]]:
    rows = _query_rows(
        conn,
        """
        WITH post_activity AS (
          SELECT
            OwnerUserId AS user_id,
            substr(CreaionDate, 1, 4) AS activity_year,
            CASE
              WHEN PostTypeId = 1 AND AcceptedAnswerId IS NOT NULL THEN 'question_accepted'
              WHEN PostTypeId = 1 AND ClosedDate IS NOT NULL THEN 'question_closed'
              WHEN PostTypeId = 1 THEN 'question_open'
              WHEN PostTypeId = 2 THEN 'answer'
              ELSE 'other_post'
            END AS bucket,
            count(*) AS activity_count,
            sum(coalesce(Score, 0)) AS score_total
          FROM posts
          WHERE OwnerUserId IS NOT NULL
          GROUP BY OwnerUserId, substr(CreaionDate, 1, 4), bucket
        ),
        comment_activity AS (
          SELECT
            UserId AS user_id,
            substr(CreationDate, 1, 4) AS activity_year,
            'comment' AS bucket,
            count(*) AS activity_count,
            sum(coalesce(Score, 0)) AS score_total
          FROM comments
          WHERE UserId IS NOT NULL
          GROUP BY UserId, substr(CreationDate, 1, 4)
        )
        SELECT * FROM post_activity
        UNION ALL
        SELECT * FROM comment_activity
        ORDER BY user_id, activity_year, bucket
        """,
    )
    out: dict[Any, dict[str, Any]] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        year = str(row.get("activity_year") or "unknown")
        bucket = str(row.get("bucket") or "unknown")
        out[row.get("user_id")][year][bucket] = {
            "count": row.get("activity_count") or 0,
            "score_total": row.get("score_total") or 0,
            "presence_state": "present",
        }
    return {user_id: {"by_year": dict(years)} for user_id, years in out.items()}


def _user_badge_buckets(conn: Any) -> dict[Any, dict[str, dict[str, Any]]]:
    rows = _query_rows(
        conn,
        """
        SELECT
          UserId,
          Name,
          count(*) AS badge_count,
          min(Date) AS first_awarded_at,
          max(Date) AS last_awarded_at
        FROM badges
        GROUP BY UserId, Name
        ORDER BY UserId, Name
        """,
    )
    out: dict[Any, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        name = str(row.get("Name") or "unknown_badge")
        out[row.get("UserId")][name] = {
            "badge_name": name,
            "count": row.get("badge_count") or 0,
            "first_awarded_at": row.get("first_awarded_at"),
            "last_awarded_at": row.get("last_awarded_at"),
        }
    return dict(out)


def _tag_thread_index(questions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for question in questions:
        for tag in _parse_tags(question.get("Tags")):
            out[tag].append(
                {
                    "question_id": question.get("Id"),
                    "title": question.get("Title"),
                    "year": _year(question.get("CreaionDate")),
                    "status_bucket": _question_status_bucket(question),
                    "score": question.get("Score") or 0,
                    "answer_count": question.get("AnswerCount") or 0,
                }
            )
    return dict(out)


def _community_thread_doc(
    question: dict[str, Any],
    *,
    users_by_id: dict[Any, dict[str, Any]],
    tags_by_name: dict[str, dict[str, Any]],
    answers: list[dict[str, Any]],
    posts_by_id: dict[Any, dict[str, Any]],
    vote_buckets: dict[Any, dict[str, dict[str, Any]]],
    comment_years: dict[Any, dict[str, dict[str, Any]]],
    comment_samples: dict[Any, list[dict[str, Any]]],
    history_years: dict[Any, dict[str, dict[str, Any]]],
    link_buckets: dict[Any, dict[str, list[dict[str, Any]]]],
) -> dict[str, Any]:
    question_id = question.get("Id")
    owner = users_by_id.get(question.get("OwnerUserId"), {})
    tag_names = _parse_tags(question.get("Tags"))
    answer_items = [
        _answer_item(
            answer,
            user=users_by_id.get(answer.get("OwnerUserId"), {}),
            votes=vote_buckets.get(answer.get("Id"), {}),
            comments=comment_years.get(answer.get("Id"), {}),
            accepted_answer_id=question.get("AcceptedAnswerId"),
        )
        for answer in sorted(answers, key=lambda row: (-(row.get("Score") or 0), row.get("Id") or 0))
    ]
    return {
        "identity": {
            "thread_id": f"question:{question_id}",
            "question_id": question_id,
            "accepted_answer_id": question.get("AcceptedAnswerId"),
            "source_post_type_id": question.get("PostTypeId"),
        },
        "question": {
            "post_id": question_id,
            "title": question.get("Title"),
            "created_at": question.get("CreaionDate"),
            "last_activity_at": question.get("LasActivityDate"),
            "score": question.get("Score") or 0,
            "view_count": question.get("ViewCount") or 0,
            "favorite_count": question.get("FavoriteCount") or 0,
            "status_bucket": _question_status_bucket(question),
            "lifecycle": {
                "closed_at": question.get("ClosedDate"),
                "last_edit_at": question.get("LastEditDate"),
                "community_owned_at": question.get("CommunityOwnedDate"),
                "last_editor": {
                    "user_id": question.get("LastEditorUserId"),
                    "display_name": question.get("LastEditorDisplayName"),
                    "presence_state": "present" if question.get("LastEditorUserId") else "missing",
                },
            },
        },
        "owner": _user_stub(owner, fallback_name=question.get("OwnerDisplayName")),
        "taxonomy": {
            "raw_tags": tag_names,
            "tags_by_name": {
                tag: {
                    "tag_name": tag,
                    "declared_count": (tags_by_name.get(tag) or {}).get("Count"),
                    "wiki_state": "present"
                    if (tags_by_name.get(tag) or {}).get("WikiPostId")
                    else "missing",
                    "threads": [
                        {
                            "thread_id": f"question:{question_id}",
                            "question_id": question_id,
                            "status_bucket": _question_status_bucket(question),
                            "year": _year(question.get("CreaionDate")),
                            "accepted_answer_state": "present"
                            if question.get("AcceptedAnswerId")
                            else "missing",
                        }
                    ],
                }
                for tag in tag_names
            },
        },
        "answers": {
            "summary": {
                "declared_count": question.get("AnswerCount") or 0,
                "materialized_count": len(answer_items),
                "accepted_answer_state": "present" if question.get("AcceptedAnswerId") else "missing",
            },
            "items": answer_items,
        },
        "comments": {
            "summary_state": "present" if comment_years.get(question_id) else "empty",
            "by_year": comment_years.get(question_id, {}),
            "highlighted": comment_samples.get(question_id, []),
        },
        "votes": {
            "state": "present" if vote_buckets.get(question_id) else "empty",
            "by_type": vote_buckets.get(question_id, {}),
        },
        "revision_timeline": {
            "state": "present" if history_years.get(question_id) else "empty",
            "by_year": history_years.get(question_id, {}),
        },
        "link_graph": {
            "state": "present" if link_buckets.get(question_id) else "empty",
            "by_type": link_buckets.get(question_id, {}),
            "accepted_answer_snapshot": _accepted_answer_snapshot(
                posts_by_id.get(question.get("AcceptedAnswerId"), {})
            ),
        },
        "observability": {
            "accepted_answer_state": "present" if question.get("AcceptedAnswerId") else "missing",
            "closed_state": "present" if question.get("ClosedDate") else "missing",
            "tag_state": "present" if tag_names else "empty",
        },
    }


def _answer_item(
    answer: dict[str, Any],
    *,
    user: dict[str, Any],
    votes: dict[str, dict[str, Any]],
    comments: dict[str, dict[str, Any]],
    accepted_answer_id: Any,
) -> dict[str, Any]:
    answer_id = answer.get("Id")
    return {
        "answer_id": answer_id,
        "created_at": answer.get("CreaionDate"),
        "score": answer.get("Score") or 0,
        "quality_bucket": _score_bucket(answer.get("Score")),
        "accepted_state": "present" if answer_id == accepted_answer_id else "missing",
        "owner": _user_stub(user, fallback_name=answer.get("OwnerDisplayName")),
        "votes_by_type": votes,
        "comments_by_year": comments,
        "lifecycle": {
            "last_activity_at": answer.get("LasActivityDate"),
            "last_edit_at": answer.get("LastEditDate"),
            "community_owned_state": "present" if answer.get("CommunityOwnedDate") else "missing",
        },
    }


def _user_reputation_profile(
    user: dict[str, Any],
    *,
    activity: dict[str, Any],
    badge_buckets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    reputation = user.get("Reputation") or 0
    return {
        "identity": {
            "user_id": user.get("Id"),
            "account_id": user.get("AccountId"),
            "display_name": user.get("DisplayName"),
        },
        "profile": {
            "created_at": user.get("CreationDate"),
            "last_access_at": user.get("LastAccessDate"),
            "location": user.get("Location"),
            "location_state": "present" if user.get("Location") else "missing",
            "age": user.get("Age"),
            "age_state": "present" if user.get("Age") is not None else "null",
        },
        "reputation": {
            "score": reputation,
            "bucket": _reputation_bucket(reputation),
            "views": user.get("Views") or 0,
            "up_votes": user.get("UpVotes") or 0,
            "down_votes": user.get("DownVotes") or 0,
        },
        "activity": activity or {"by_year": {}},
        "badge_ecosystem": {
            "state": "present" if badge_buckets else "empty",
            "by_name": badge_buckets,
        },
    }


def _tag_topic_ecosystem(
    tag: dict[str, Any],
    *,
    thread_refs: list[dict[str, Any]],
    posts_by_id: dict[Any, dict[str, Any]],
) -> dict[str, Any]:
    threads_by_status_by_year: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {"threads": []}))
    for ref in sorted(thread_refs, key=lambda item: (item["year"], item["question_id"])):
        status = str(ref["status_bucket"])
        year = str(ref["year"])
        threads_by_status_by_year[status][year]["threads"].append(ref)
    excerpt = posts_by_id.get(tag.get("ExcerptPostId"), {})
    wiki = posts_by_id.get(tag.get("WikiPostId"), {})
    return {
        "identity": {
            "tag_id": tag.get("Id"),
            "tag_name": tag.get("TagName"),
        },
        "topic": {
            "declared_count": tag.get("Count") or 0,
            "count_bucket": _tag_count_bucket(tag.get("Count")),
            "excerpt_state": "present" if excerpt else "missing",
            "wiki_state": "present" if wiki else "missing",
        },
        "threads_by_status_by_year": {
            status: dict(years) for status, years in threads_by_status_by_year.items()
        },
        "wiki_posts": {
            "excerpt": _tag_wiki_stub(excerpt),
            "wiki": _tag_wiki_stub(wiki),
        },
    }


def _accepted_answer_snapshot(answer: dict[str, Any]) -> dict[str, Any]:
    if not answer:
        return {"state": "missing"}
    return {
        "state": "present",
        "answer_id": answer.get("Id"),
        "score": answer.get("Score") or 0,
        "created_at": answer.get("CreaionDate"),
        "owner_user_id": answer.get("OwnerUserId"),
    }


def _tag_wiki_stub(post: dict[str, Any]) -> dict[str, Any]:
    if not post:
        return {"state": "missing"}
    return {
        "state": "present",
        "post_id": post.get("Id"),
        "score": post.get("Score") or 0,
        "last_activity_at": post.get("LasActivityDate"),
    }


def _user_stub(user: dict[str, Any], *, fallback_name: Any = None) -> dict[str, Any]:
    if not user:
        return {
            "user_id": None,
            "display_name": fallback_name,
            "presence_state": "missing" if fallback_name is None else "present",
            "reputation_bucket": "unknown",
        }
    return {
        "user_id": user.get("Id"),
        "display_name": user.get("DisplayName") or fallback_name,
        "presence_state": "present",
        "reputation_bucket": _reputation_bucket(user.get("Reputation") or 0),
    }


def _parse_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    return [match.group(1) for match in _TAG_RE.finditer(str(raw))]


def _question_status_bucket(question: dict[str, Any]) -> str:
    accepted = question.get("AcceptedAnswerId") is not None
    closed = question.get("ClosedDate") is not None
    if accepted and closed:
        return "accepted_closed"
    if accepted:
        return "accepted_open"
    if closed:
        return "unanswered_closed"
    return "unanswered_open"


def _score_bucket(score: Any) -> str:
    value = int(score or 0)
    if value >= 10:
        return "high_score"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"


def _reputation_bucket(reputation: int) -> str:
    if reputation >= 10000:
        return "trusted_high_reputation"
    if reputation >= 1000:
        return "established"
    if reputation >= 100:
        return "participant"
    return "new_or_low_reputation"


def _tag_count_bucket(count: Any) -> str:
    value = int(count or 0)
    if value >= 1000:
        return "major_topic"
    if value >= 100:
        return "active_topic"
    if value >= 10:
        return "niche_topic"
    return "rare_topic"


def _year(value: Any) -> str:
    text = str(value or "")
    return text[:4] if len(text) >= 4 else "unknown"


def _source_tables_from_refs(refs: list[str]) -> list[str]:
    return sorted({ref.split(".", 1)[0] for ref in refs if "." in ref})


def _manifest() -> NativeFeatureManifest:
    return NativeFeatureManifest(
        db_id="codebase_community",
        features=[
            NativeFeature(
                id="community_threads.tag_thread_matrix",
                type="dynamic_key_object",
                collection="community_threads",
                field="taxonomy.tags_by_name",
                query_patterns=["community_tag_thread_matrix"],
                required_constructs=["$objectToArray", "$unwind", "$group", "$sum"],
                provenance_refs=["posts.Id", "posts.Tags", "posts.AcceptedAnswerId", "tags.TagName", "tags.Count"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "community_tag_thread_matrix",
                            "intent": "traverse dynamic tag keys and count question-thread states by tag",
                            "pipeline": [
                                {"$project": {"question_id": "$identity.question_id", "tags": {"$objectToArray": "$taxonomy.tags_by_name"}}},
                                {"$unwind": "$tags"},
                                {"$unwind": "$tags.v.threads"},
                                {"$group": {"_id": {"tag": "$tags.k", "status": "$tags.v.threads.status_bucket"}, "thread_count": {"$sum": 1}}},
                                {"$sort": {"thread_count": -1, "_id.tag": 1}},
                                {"$limit": 50},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="community_threads.answer_vote_buckets",
                type="array_object_dynamic_keys",
                collection="community_threads",
                field="answers.items.votes_by_type",
                query_patterns=["community_answer_vote_bucket_scan"],
                required_constructs=["$unwind", "$objectToArray", "$filter", "$sum"],
                provenance_refs=["posts.Id", "posts.ParentId", "votes.PostId", "votes.VoteTypeId", "votes.BountyAmount"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "community_answer_vote_bucket_scan",
                            "intent": "scan answer arrays and explode each answer's dynamic vote-type buckets",
                            "pipeline": [
                                {"$unwind": "$answers.items"},
                                {"$project": {"question_id": "$identity.question_id", "answer_id": "$answers.items.answer_id", "vote_types": {"$objectToArray": "$answers.items.votes_by_type"}}},
                                {"$addFields": {"positive_vote_types": {"$filter": {"input": "$vote_types", "as": "vote", "cond": {"$gt": ["$$vote.v.count", 0]}}}}},
                                {"$unwind": "$positive_vote_types"},
                                {"$group": {"_id": "$positive_vote_types.k", "answer_count": {"$sum": 1}, "vote_total": {"$sum": "$positive_vote_types.v.count"}}},
                                {"$sort": {"vote_total": -1}},
                                {"$limit": 50},
                            ],
                            "mongo_native_constructs": ["$unwind", "$objectToArray", "$filter", "$group", "$sum"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="user_reputation_profiles.reputation_activity_lattice",
                type="dynamic_key_object",
                collection="user_reputation_profiles",
                field="activity.by_year",
                query_patterns=["community_user_reputation_activity_lattice"],
                required_constructs=["$objectToArray", "$unwind", "$group", "$avg"],
                provenance_refs=["users.Id", "users.Reputation", "posts.OwnerUserId", "posts.CreaionDate", "comments.UserId"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "community_user_reputation_activity_lattice",
                            "intent": "compare reputation buckets across year-keyed user activity lattices",
                            "pipeline": [
                                {"$project": {"user_id": "$identity.user_id", "reputation_bucket": "$reputation.bucket", "years": {"$objectToArray": "$activity.by_year"}}},
                                {"$unwind": "$years"},
                                {"$project": {"reputation_bucket": 1, "year": "$years.k", "activity_buckets": {"$objectToArray": "$years.v"}}},
                                {"$unwind": "$activity_buckets"},
                                {"$group": {"_id": {"reputation_bucket": "$reputation_bucket", "bucket": "$activity_buckets.k"}, "avg_events": {"$avg": "$activity_buckets.v.count"}, "users": {"$sum": 1}}},
                                {"$sort": {"users": -1}},
                                {"$limit": 50},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$avg"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="tag_topic_ecosystems.topic_status_year_buckets",
                type="dynamic_key_object",
                collection="tag_topic_ecosystems",
                field="threads_by_status_by_year",
                query_patterns=["community_topic_status_year_buckets"],
                required_constructs=["$objectToArray", "$unwind", "$size", "$group"],
                provenance_refs=["tags.TagName", "tags.Count", "posts.Tags", "posts.ClosedDate", "posts.AcceptedAnswerId"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "community_topic_status_year_buckets",
                            "intent": "walk topic status buckets and compare year-keyed thread arrays",
                            "pipeline": [
                                {"$project": {"tag": "$identity.tag_name", "statuses": {"$objectToArray": "$threads_by_status_by_year"}}},
                                {"$unwind": "$statuses"},
                                {"$project": {"tag": 1, "status": "$statuses.k", "years": {"$objectToArray": "$statuses.v"}}},
                                {"$unwind": "$years"},
                                {"$project": {"tag": 1, "status": 1, "year": "$years.k", "thread_count": {"$size": "$years.v.threads"}}},
                                {"$group": {"_id": {"status": "$status", "year": "$year"}, "tags": {"$sum": 1}, "threads": {"$sum": "$thread_count"}}},
                                {"$sort": {"threads": -1}},
                                {"$limit": 50},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$size", "$group", "$sum"],
                        }
                    ]
                },
            ),
        ],
    )
