"""本地状态存储：checkpoint、人设快照、待审提案、压缩日志。"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

try:
    import aiosqlite
except ImportError:
    aiosqlite = None


class Store:
    """插件本地 SQLite 存储。

    存储内容：
    - patch_checkpoints: 每个 persona 的最后处理记忆 id（增量读取用）
    - persona_snapshots: 人设写回前的快照（回滚用）
    - pending_proposals: 待审批的人设变更提案
    - compact_log: 记忆压缩执行日志
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: Any = None

    async def initialize(self) -> None:
        if aiosqlite is None:
            logger.warning("[LMPatch] aiosqlite 未安装，本地状态存储不可用")
            return
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        await self._db.commit()
        logger.info("[LMPatch] 本地状态存储已初始化")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _create_tables(self) -> None:
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS patch_checkpoints (
                persona_id TEXT PRIMARY KEY,
                last_memory_id INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS persona_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id TEXT NOT NULL,
                persona_name TEXT,
                snapshot_text TEXT NOT NULL,
                created_at REAL NOT NULL,
                trigger_memory_ids TEXT,
                change_description TEXT,
                proposal_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS pending_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id TEXT NOT NULL,
                persona_name TEXT,
                original_persona TEXT NOT NULL,
                proposed_persona TEXT NOT NULL,
                change_description TEXT,
                changed_aspects TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                rejection_reason TEXT,
                reroll_count INTEGER NOT NULL DEFAULT 0,
                trigger_memory_ids TEXT,
                is_init INTEGER NOT NULL DEFAULT 0,
                init_batch INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_proposals_status
                ON pending_proposals(status);

            CREATE INDEX IF NOT EXISTS idx_proposals_persona
                ON pending_proposals(persona_id);

            CREATE TABLE IF NOT EXISTS compact_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id TEXT NOT NULL,
                deleted_ids TEXT NOT NULL,
                created_ids TEXT NOT NULL,
                deleted_count INTEGER NOT NULL,
                created_count INTEGER NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_persona
                ON persona_snapshots(persona_id);

            CREATE TABLE IF NOT EXISTS init_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                type TEXT,
                status TEXT NOT NULL DEFAULT 'idle',
                current_persona_id TEXT,
                current_persona_idx INTEGER NOT NULL DEFAULT 0,
                current_batch INTEGER NOT NULL DEFAULT 0,
                total_personas INTEGER NOT NULL DEFAULT 0,
                processed_until_id INTEGER NOT NULL DEFAULT 0,
                total_processed INTEGER NOT NULL DEFAULT 0,
                total_compacted INTEGER NOT NULL DEFAULT 0,
                started_at REAL,
                updated_at REAL,
                finished_at REAL,
                error TEXT
            );

            INSERT OR IGNORE INTO init_state (id, status) VALUES (1, 'idle');
        """)
        # Migration: 为已存在的 pending_proposals 表补齐新列
        await self._migrate_proposals_table()

    async def _migrate_proposals_table(self) -> None:
        """为旧版本的 pending_proposals 表添加 is_init/init_batch 列。"""
        cursor = await self._db.execute("PRAGMA table_info(pending_proposals)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "is_init" not in columns:
            await self._db.execute(
                "ALTER TABLE pending_proposals ADD COLUMN is_init INTEGER NOT NULL DEFAULT 0"
            )
        if "init_batch" not in columns:
            await self._db.execute(
                "ALTER TABLE pending_proposals ADD COLUMN init_batch INTEGER NOT NULL DEFAULT 0"
            )

    # ------------------------------------------------------------------
    # Checkpoint（增量读取）
    # ------------------------------------------------------------------

    async def get_checkpoint(self, persona_id: str) -> int:
        """获取指定 persona 的最后处理记忆 id，不存在返回 0。"""
        if self._db is None:
            return 0
        cursor = await self._db.execute(
            "SELECT last_memory_id FROM patch_checkpoints WHERE persona_id = ?",
            (persona_id,),
        )
        row = await cursor.fetchone()
        return row["last_memory_id"] if row else 0

    async def update_checkpoint(self, persona_id: str, last_memory_id: int) -> None:
        if self._db is None:
            return
        await self._db.execute(
            "INSERT INTO patch_checkpoints (persona_id, last_memory_id, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(persona_id) DO UPDATE SET "
            "last_memory_id = excluded.last_memory_id, updated_at = excluded.updated_at",
            (persona_id, last_memory_id, time.time()),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # 人设快照（回滚用）
    # ------------------------------------------------------------------

    async def save_snapshot(
        self,
        persona_id: str,
        persona_name: str | None,
        snapshot_text: str,
        trigger_memory_ids: list[int] | None = None,
        change_description: str | None = None,
        proposal_id: int | None = None,
    ) -> int:
        """保存人设快照，返回快照 id。"""
        if self._db is None:
            return -1
        cursor = await self._db.execute(
            "INSERT INTO persona_snapshots "
            "(persona_id, persona_name, snapshot_text, created_at, "
            " trigger_memory_ids, change_description, proposal_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                persona_id,
                persona_name,
                snapshot_text,
                time.time(),
                json.dumps(trigger_memory_ids) if trigger_memory_ids else None,
                change_description,
                proposal_id,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def list_snapshots(
        self, persona_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        if persona_id:
            cursor = await self._db.execute(
                "SELECT * FROM persona_snapshots WHERE persona_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (persona_id, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM persona_snapshots ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        if self._db is None:
            return None
        cursor = await self._db.execute(
            "SELECT * FROM persona_snapshots WHERE id = ?",
            (snapshot_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 待审提案
    # ------------------------------------------------------------------

    async def add_proposal(
        self,
        persona_id: str,
        persona_name: str | None,
        original_persona: str,
        proposed_persona: str,
        change_description: str | None = None,
        changed_aspects: list[str] | None = None,
        trigger_memory_ids: list[int] | None = None,
        reroll_count: int = 0,
        is_init: bool = False,
        init_batch: int = 0,
    ) -> int:
        """添加一个待审提案，返回提案 id。"""
        if self._db is None:
            return -1
        now = time.time()
        cursor = await self._db.execute(
            "INSERT INTO pending_proposals "
            "(persona_id, persona_name, original_persona, proposed_persona, "
            " change_description, changed_aspects, status, reroll_count, "
            " trigger_memory_ids, is_init, init_batch, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (
                persona_id,
                persona_name,
                original_persona,
                proposed_persona,
                change_description,
                json.dumps(changed_aspects) if changed_aspects else None,
                reroll_count,
                json.dumps(trigger_memory_ids) if trigger_memory_ids else None,
                1 if is_init else 0,
                init_batch,
                now,
                now,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def list_pending_proposals(self) -> list[dict[str, Any]]:
        """列出所有待审提案。"""
        if self._db is None:
            return []
        cursor = await self._db.execute(
            "SELECT * FROM pending_proposals WHERE status = 'pending' "
            "ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [self._format_proposal(row) for row in rows]

    async def list_all_proposals(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """列出所有提案（含历史），可按状态过滤。"""
        if self._db is None:
            return []
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM pending_proposals WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM pending_proposals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [self._format_proposal(row) for row in rows]

    async def get_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        if self._db is None:
            return None
        cursor = await self._db.execute(
            "SELECT * FROM pending_proposals WHERE id = ?",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        return self._format_proposal(row) if row else None

    async def update_proposal_status(
        self,
        proposal_id: int,
        status: str,
        rejection_reason: str | None = None,
    ) -> None:
        """更新提案状态。status: approved / rejected / stalled。"""
        if self._db is None:
            return
        await self._db.execute(
            "UPDATE pending_proposals SET status = ?, rejection_reason = ?, "
            "updated_at = ? WHERE id = ?",
            (status, rejection_reason, time.time(), proposal_id),
        )
        await self._db.commit()

    async def update_proposal_content(
        self,
        proposal_id: int,
        proposed_persona: str,
        change_description: str | None = None,
        changed_aspects: list[str] | None = None,
    ) -> None:
        """更新提案内容（打回重提议时用）。"""
        if self._db is None:
            return
        await self._db.execute(
            "UPDATE pending_proposals SET proposed_persona = ?, "
            "change_description = ?, changed_aspects = ?, updated_at = ? "
            "WHERE id = ?",
            (
                proposed_persona,
                change_description,
                json.dumps(changed_aspects) if changed_aspects else None,
                time.time(),
                proposal_id,
            ),
        )
        await self._db.commit()

    async def increment_reroll(self, proposal_id: int) -> int:
        """递增重提议次数，返回当前次数。"""
        if self._db is None:
            return 0
        await self._db.execute(
            "UPDATE pending_proposals SET reroll_count = reroll_count + 1, "
            "updated_at = ? WHERE id = ?",
            (time.time(), proposal_id),
        )
        await self._db.commit()
        cursor = await self._db.execute(
            "SELECT reroll_count FROM pending_proposals WHERE id = ?",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        return row["reroll_count"] if row else 0

    async def reset_reroll(self, proposal_id: int) -> None:
        """重置重提议次数为 0，并将状态改回 pending（用于重启 stalled 提案）。"""
        if self._db is None:
            return
        await self._db.execute(
            "UPDATE pending_proposals SET reroll_count = 0, status = 'pending', "
            "rejection_reason = NULL, updated_at = ? WHERE id = ?",
            (time.time(), proposal_id),
        )
        await self._db.commit()

    def _format_proposal(self, row) -> dict[str, Any]:
        result = dict(row)
        if result.get("changed_aspects"):
            try:
                result["changed_aspects"] = json.loads(result["changed_aspects"])
            except Exception:
                result["changed_aspects"] = []
        else:
            result["changed_aspects"] = []
        if result.get("trigger_memory_ids"):
            try:
                result["trigger_memory_ids"] = json.loads(result["trigger_memory_ids"])
            except Exception:
                result["trigger_memory_ids"] = []
        else:
            result["trigger_memory_ids"] = []
        return result

    # ------------------------------------------------------------------
    # 压缩日志
    # ------------------------------------------------------------------

    async def log_compaction(
        self,
        persona_id: str,
        deleted_ids: list[int],
        created_ids: list[int],
    ) -> None:
        if self._db is None:
            return
        await self._db.execute(
            "INSERT INTO compact_log "
            "(persona_id, deleted_ids, created_ids, deleted_count, "
            " created_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                persona_id,
                json.dumps(deleted_ids),
                json.dumps(created_ids),
                len(deleted_ids),
                len(created_ids),
                time.time(),
            ),
        )
        await self._db.commit()

    async def list_compaction_log(
        self, persona_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        if persona_id:
            cursor = await self._db.execute(
                "SELECT * FROM compact_log WHERE persona_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (persona_id, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM compact_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("deleted_ids"):
                try:
                    d["deleted_ids"] = json.loads(d["deleted_ids"])
                except Exception:
                    d["deleted_ids"] = []
            if d.get("created_ids"):
                try:
                    d["created_ids"] = json.loads(d["created_ids"])
                except Exception:
                    d["created_ids"] = []
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # 初始化状态（人设迭代初始化 / 记忆压缩初始化，全局互斥）
    # ------------------------------------------------------------------

    async def get_init_state(self) -> dict[str, Any]:
        """获取初始化状态（单行表）。"""
        if self._db is None:
            return {"status": "idle", "type": None}
        cursor = await self._db.execute("SELECT * FROM init_state WHERE id = 1")
        row = await cursor.fetchone()
        if row is None:
            return {"status": "idle", "type": None}
        return dict(row)

    async def start_init(self, init_type: str, total_personas: int) -> bool:
        """尝试启动初始化。若当前非 idle 返回 False（互斥）。"""
        if self._db is None:
            return False
        state = await self.get_init_state()
        if state.get("status") not in ("idle", "completed", "cancelled"):
            return False
        now = time.time()
        await self._db.execute(
            "UPDATE init_state SET "
            "type = ?, status = 'running', current_persona_id = NULL, "
            "current_persona_idx = 0, current_batch = 0, "
            "total_personas = ?, processed_until_id = 0, "
            "total_processed = 0, total_compacted = 0, "
            "started_at = ?, updated_at = ?, finished_at = NULL, error = NULL "
            "WHERE id = 1",
            (init_type, total_personas, now, now),
        )
        await self._db.commit()
        return True

    async def update_init_state(self, **fields) -> None:
        """更新初始化状态的指定字段。"""
        if self._db is None or not fields:
            return
        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        params = tuple(fields.values())
        await self._db.execute(
            f"UPDATE init_state SET {set_clause} WHERE id = 1", params
        )
        await self._db.commit()

    async def complete_init(
        self, total_processed: int, total_compacted: int = 0
    ) -> None:
        """标记初始化完成。"""
        if self._db is None:
            return
        now = time.time()
        await self._db.execute(
            "UPDATE init_state SET status = 'completed', "
            "total_processed = ?, total_compacted = ?, "
            "finished_at = ?, updated_at = ? WHERE id = 1",
            (total_processed, total_compacted, now, now),
        )
        await self._db.commit()

    async def cancel_init(self, error: str = "") -> None:
        """取消初始化（用户主动取消或出错）。"""
        if self._db is None:
            return
        now = time.time()
        await self._db.execute(
            "UPDATE init_state SET status = 'cancelled', error = ?, "
            "finished_at = ?, updated_at = ? WHERE id = 1",
            (error, now, now),
        )
        await self._db.commit()

    async def reset_init_to_idle(self) -> None:
        """将初始化状态重置为 idle（用于取消后重新开始）。"""
        if self._db is None:
            return
        await self._db.execute(
            "UPDATE init_state SET status = 'idle', type = NULL, "
            "current_persona_id = NULL, error = NULL, "
            "updated_at = ? WHERE id = 1",
            (time.time(),),
        )
        await self._db.commit()
