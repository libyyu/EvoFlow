"""Repository for role push bindings."""

from __future__ import annotations

import logging
from typing import Any
from datetime import datetime
import sqlite3

from .push_policy import RolePushPolicy, ChannelBinding, ChannelType

logger = logging.getLogger(__name__)


class RolePushBindingRepository:
    """Read/write role push bindings from evoflow_role_push_bindings table."""
    
    @staticmethod
    def _get_db() -> sqlite3.Connection:
        """Get database connection."""
        from evoflow.runtime.ports import get_app_db
        db = get_app_db()
        if db is None:
            raise RuntimeError("Application database not available")
        return db
    
    @classmethod
    def ensure_table(cls) -> None:
        """Ensure evoflow_role_push_bindings table exists (idempotent)."""
        db = cls._get_db()
        cur = db.cursor()
        # Table creation is handled by schema.py, but this is a safety check
        cur.execute("""
            CREATE TABLE IF NOT EXISTS evoflow_role_push_bindings (
                agent_code TEXT PRIMARY KEY,
                push_enabled INTEGER NOT NULL DEFAULT 1,
                push_channels TEXT NOT NULL DEFAULT 'feishu',
                feishu_open_id TEXT NOT NULL DEFAULT '',
                feishu_chat_id TEXT NOT NULL DEFAULT '',
                feishu_user_id TEXT NOT NULL DEFAULT '',
                feishu_target_type TEXT NOT NULL DEFAULT 'open_id',
                feishu_verified_at TEXT,
                wecom_bot_id TEXT NOT NULL DEFAULT '',
                wecom_verified_at TEXT,
                slack_channel_id TEXT NOT NULL DEFAULT '',
                slack_verified_at TEXT,
                email TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (agent_code) REFERENCES evoflow_proactive_roles(agent_code) ON DELETE CASCADE
            );
        """)
        db.commit()
    
    @classmethod
    def get_policy(cls, agent_code: str) -> RolePushPolicy | None:
        """Load policy from database, or None if not found."""
        try:
            db = cls._get_db()
            cur = db.cursor()
            cur.execute(
                "SELECT * FROM evoflow_role_push_bindings WHERE agent_code = ?",
                (agent_code,)
            )
            row = cur.fetchone()
            if not row:
                return None
            
            policy = RolePushPolicy(
                agent_code=agent_code,
                push_enabled=bool(row[1]),  # push_enabled
                channels={},
                created_at=row[13],
                updated_at=row[14],
            )
            
            # Parse channel bindings from columns
            _load_channels_into_policy(policy, row)
            return policy
        except Exception as e:
            logger.warning("Failed to load push policy for %s: %s", agent_code, e, exc_info=True)
            return None
    
    @classmethod
    def get_or_create_policy(cls, agent_code: str) -> RolePushPolicy:
        """Load policy, or create a default one if not found."""
        policy = cls.get_policy(agent_code)
        if policy is not None:
            return policy
        
        # Create default (no channels enabled)
        policy = RolePushPolicy.default_for_role(agent_code)
        try:
            cls.save_policy(policy)
        except Exception as e:
            logger.warning("Failed to create default policy for %s: %s", agent_code, e)
        
        return policy
    
    @classmethod
    def save_policy(cls, policy: RolePushPolicy) -> None:
        """Save policy to database."""
        try:
            db = cls._get_db()
            cur = db.cursor()
            
            # Build channel columns from policy
            feishu_open_id = ""
            feishu_chat_id = ""
            feishu_user_id = ""
            feishu_target_type = "open_id"
            feishu_verified_at = None
            wecom_bot_id = ""
            wecom_verified_at = None
            slack_channel_id = ""
            slack_verified_at = None
            email = ""
            
            if "feishu" in policy.channels:
                b = policy.channels["feishu"]
                feishu_open_id = b.target_id if b.target_type == "open_id" else ""
                feishu_chat_id = b.target_id if b.target_type == "chat_id" else ""
                feishu_user_id = b.target_id if b.target_type == "user_id" else ""
                feishu_target_type = b.target_type or "open_id"
                feishu_verified_at = b.verified_at
            
            if "wecom" in policy.channels:
                b = policy.channels["wecom"]
                wecom_bot_id = b.target_id
                wecom_verified_at = b.verified_at
            
            if "slack" in policy.channels:
                b = policy.channels["slack"]
                slack_channel_id = b.target_id
                slack_verified_at = b.verified_at
            
            if "email" in policy.channels:
                b = policy.channels["email"]
                email = b.target_id
            
            push_channels_str = ",".join(ch for ch in policy.channels if policy.channels[ch].enabled)
            
            cur.execute("""
                INSERT INTO evoflow_role_push_bindings (
                    agent_code, push_enabled, push_channels,
                    feishu_open_id, feishu_chat_id, feishu_user_id, feishu_target_type, feishu_verified_at,
                    wecom_bot_id, wecom_verified_at,
                    slack_channel_id, slack_verified_at,
                    email,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_code) DO UPDATE SET
                    push_enabled = excluded.push_enabled,
                    push_channels = excluded.push_channels,
                    feishu_open_id = excluded.feishu_open_id,
                    feishu_chat_id = excluded.feishu_chat_id,
                    feishu_user_id = excluded.feishu_user_id,
                    feishu_target_type = excluded.feishu_target_type,
                    feishu_verified_at = excluded.feishu_verified_at,
                    wecom_bot_id = excluded.wecom_bot_id,
                    wecom_verified_at = excluded.wecom_verified_at,
                    slack_channel_id = excluded.slack_channel_id,
                    slack_verified_at = excluded.slack_verified_at,
                    email = excluded.email,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
            """, (
                policy.agent_code,
                1 if policy.push_enabled else 0,
                push_channels_str,
                feishu_open_id,
                feishu_chat_id,
                feishu_user_id,
                feishu_target_type,
                feishu_verified_at,
                wecom_bot_id,
                wecom_verified_at,
                slack_channel_id,
                slack_verified_at,
                email,
                policy.created_at,
                policy.updated_at,
            ))
            db.commit()
        except Exception as e:
            logger.error("Failed to save push policy for %s: %s", policy.agent_code, e, exc_info=True)
            raise
    
    @classmethod
    def list_policies(cls) -> list[RolePushPolicy]:
        """Load all push policies."""
        try:
            db = cls._get_db()
            cur = db.cursor()
            cur.execute("SELECT * FROM evoflow_role_push_bindings ORDER BY agent_code")
            policies = []
            for row in cur.fetchall():
                agent_code = row[0]
                policy = RolePushPolicy(
                    agent_code=agent_code,
                    push_enabled=bool(row[1]),
                    channels={},
                    created_at=row[13],
                    updated_at=row[14],
                )
                _load_channels_into_policy(policy, row)
                policies.append(policy)
            return policies
        except Exception as e:
            logger.warning("Failed to list push policies: %s", e, exc_info=True)
            return []
    
    @classmethod
    def delete_policy(cls, agent_code: str) -> None:
        """Delete a policy."""
        try:
            db = cls._get_db()
            cur = db.cursor()
            cur.execute("DELETE FROM evoflow_role_push_bindings WHERE agent_code = ?", (agent_code,))
            db.commit()
        except Exception as e:
            logger.error("Failed to delete push policy for %s: %s", agent_code, e, exc_info=True)
            raise
    
    @classmethod
    def mark_verified(cls, agent_code: str, channel: ChannelType) -> None:
        """Mark a channel binding as verified."""
        policy = cls.get_policy(agent_code)
        if policy:
            policy.mark_verified(channel)
            cls.save_policy(policy)


def _load_channels_into_policy(policy: RolePushPolicy, row: tuple) -> None:
    """Parse database row columns into policy.channels dict."""
    # row: (agent_code, push_enabled, push_channels, feishu_open_id, feishu_chat_id, feishu_user_id, 
    #        feishu_target_type, feishu_verified_at, wecom_bot_id, wecom_verified_at, 
    #        slack_channel_id, slack_verified_at, email, created_at, updated_at)
    
    feishu_open_id = (row[3] or "").strip()
    feishu_chat_id = (row[4] or "").strip()
    feishu_user_id = (row[5] or "").strip()
    feishu_target_type = (row[6] or "open_id").strip()
    feishu_verified_at = row[7]
    
    wecom_bot_id = (row[8] or "").strip()
    wecom_verified_at = row[9]
    
    slack_channel_id = (row[10] or "").strip()
    slack_verified_at = row[11]
    
    email = (row[12] or "").strip()
    
    # Reconstruct feishu binding
    if feishu_open_id or feishu_chat_id or feishu_user_id:
        target_id = feishu_open_id or feishu_chat_id or feishu_user_id
        policy.channels["feishu"] = ChannelBinding(
            channel="feishu",
            enabled=True,
            target_id=target_id,
            target_type=feishu_target_type,
            verified_at=feishu_verified_at,
        )
    
    # Reconstruct wecom binding
    if wecom_bot_id:
        policy.channels["wecom"] = ChannelBinding(
            channel="wecom",
            enabled=True,
            target_id=wecom_bot_id,
            target_type="bot_id",
            verified_at=wecom_verified_at,
        )
    
    # Reconstruct slack binding
    if slack_channel_id:
        policy.channels["slack"] = ChannelBinding(
            channel="slack",
            enabled=True,
            target_id=slack_channel_id,
            target_type="channel_id",
            verified_at=slack_verified_at,
        )
    
    # Reconstruct email binding
    if email:
        policy.channels["email"] = ChannelBinding(
            channel="email",
            enabled=True,
            target_id=email,
            target_type="email",
            verified_at=None,
        )
