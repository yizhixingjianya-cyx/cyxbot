"""指令统计 & QQ 官方指令面板差分同步

- collect_registered_commands(): 扫描 NoneBot 已加载插件的 on_command（含别名），
  面板指令名取「触发词+别名」里最短的那个（“有别名只取最短”）。
- load_merge_commands(): 与 configs/commands.json 合并（自动扫描结果 + 手工维护的
  desc/scope/block/manual 字段，纯关键词/未扫描到的指令以 manual 方式保留）。
- sync_all_goqbot_panels(bot): goqbot 连接后调用：
    先 pull 平台该 scope 的指令面板 → 与本地差分，多的删少的补；
    本地标记 block=true 的不上传（若面板里已有同名旧条目会随“多的删”一并移除）；
    无合适全局面板时自动 create，超过 20 条自动分面板。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from nonebot import get_loaded_plugins

from .env_value import text as env_text
from .gobot_api import (
    PlatformNotSupportedError,
    create_command_panel,
    delete_command_panel,
    get_command_panel,
    get_command_panels,
    is_goqbot,
    set_command_panel,
)

logger = logging.getLogger("util.command_sync")

# 面板默认生效场景（QQ 官方：group=群 / c2c=单聊）
SYNC_SCOPES: List[str] = ["group", "c2c"]
# 每页拉取面板数
_PAGE_LIMIT = 100
# 单面板最多 20 个指令元素（QQ 官方限制）
_MAX_ITEMS_PER_PANEL = 20
# 指令按钮名称最多 14 显示宽度（1 个汉字算 2）/ desc 最多 30
_MAX_NAME_WIDTH = 14
_MAX_DESC_WIDTH = 30


# ---------------------------------------------------------------- 路径与存取
def commands_json_path() -> Path:
    """commands.json 路径，可用环境变量 COMMANDS_JSON_PATH 覆盖"""
    p = env_text("COMMANDS_JSON_PATH")
    return Path(p) if p else Path("configs") / "commands.json"


def _display_width(text: str) -> int:
    """按 goqbot 口径粗略算显示宽度：汉字/全角算 2，其余算 1"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _clip_display(text: Any, limit: int) -> str:
    """把 desc 等纯展示文本截断到指定显示宽度，避免触发平台参数校验"""
    s = str(text or "").strip()
    out, width = [], 0
    for ch in s:
        w = 2 if ord(ch) > 0x2E7F else 1
        if width + w > limit:
            break
        out.append(ch)
        width += w
    return "".join(out)


def _empty_doc() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "sync_enabled": True,  # 顶层总开关：false 时 cmd_sync 不做任何同步
        # 插件白名单：只把名单内插件(on_command 扫描结果按 plugin 归属)的指令同步上传，
        # 其它插件指令即使被扫描到也强制 block（记录保留、上传跳过、面板里已有会差删）。
        # 留空/缺省 = 不过滤（沿用 block 手工字段）
        "sync_plugins": [],
        "scopes": list(SYNC_SCOPES),
        "commands": [],
        "summary": {},
    }


def load_commands_doc() -> Dict[str, Any]:
    path = commands_json_path()
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("commands"), list):
                doc.setdefault("sync_enabled", True)
                doc.setdefault("sync_plugins", [])
                return doc
        except Exception:
            logger.warning(f"commands.json 解析失败，将重新生成: {path}")
    return _empty_doc()


def _summarize(doc: Dict[str, Any]) -> Dict[str, Any]:
    """统计指令清单，写入 doc['summary']"""
    cmds = [c for c in doc.get("commands", []) if isinstance(c, dict) and c.get("name")]
    by_plugin: Dict[str, int] = {}
    for c in cmds:
        by_plugin[str(c.get("plugin") or "manual")] = (
            by_plugin.get(str(c.get("plugin") or "manual"), 0) + 1
        )
    return {
        "total": len(cmds),
        "blocked": sum(1 for c in cmds if c.get("block")),
        "manual": sum(1 for c in cmds if c.get("manual")),
        "with_aliases": sum(1 for c in cmds if c.get("aliases")),
        "by_plugin": dict(sorted(by_plugin.items(), key=lambda kv: -kv[1])),
    }


def save_commands_doc(doc: Dict[str, Any]) -> Path:
    path = commands_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )
    if isinstance(doc.get("commands"), list):
        doc["summary"] = _summarize(doc)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 自动扫描
def _extract_command_parts(checkers: Sequence[Any]) -> Optional[List[Tuple[str, ...]]]:
    """从 matcher.rule.checkers 中找出 on_command 的 CommandRule，返回全部触发词

    NoneBot 中 on_command 匹配规则会以一个带 `cmds`（tuple[str,...] 列表）的
    规则对象形式存进 rule.checkers，规则对象又被包在 Dependent 里（.call 取原对象）。
    不同版本类名可能是 Command/CommandRule/ShellCommandRule，这里按结构鸭子判断。
    """
    for checker in checkers or ():
        call = getattr(checker, "call", None) or checker
        cmds = getattr(call, "cmds", None)
        if not isinstance(cmds, (list, tuple)) or not cmds:
            continue
        if not all(
            isinstance(parts, tuple) and parts and all(isinstance(w, str) for w in parts)
            for parts in cmds
        ):
            continue
        parts_list: List[Tuple[str, ...]] = []
        for parts in cmds:
            if parts:
                parts_list.append(parts)
        if parts_list:
            return parts_list
    return None


def collect_registered_commands() -> List[Dict[str, Any]]:
    """扫描所有已加载插件中 on_command 注册的指令

    返回元素: {name, aliases, plugin, desc, scope, block, manual}
    name = 该指令的主指令名（on_command 第一个参数）。主指令信息在注册时由
    util.command_registry 记录（NoneBot 规则里只剩无序集合还原不了）；个别没被
    记录到的规则退化为取最短触发词（“有别名只取最短”的兜底）。
    """
    from .command_registry import resolve_primary

    found: Dict[str, Dict[str, Any]] = {}
    for plugin in get_loaded_plugins():
        for matcher in getattr(plugin, "matcher", ()) or ():
            rule = getattr(matcher, "rule", None)
            parts_list = (
                _extract_command_parts(getattr(rule, "checkers", ()) or ())
                if rule
                else None
            )
            if not parts_list:
                continue
            texts = ["".join(p) for p in parts_list]
            texts = list(dict.fromkeys(t for t in texts if t))
            if not texts:
                continue
            # 主指令优先：本 matcher 的所有触发词应归属同一个主指令
            primaries = {resolve_primary(p) for p in parts_list}
            primaries.discard(None)
            if len(primaries) == 1:
                name = next(iter(primaries))
            else:  # 未知注册（包装前导入等），退化为最短触发词
                name = min(texts, key=lambda w: (len(w), w))
            if name in found:
                continue  # 同名指令已登记，去重
            aliases = sorted(
                (w for w in texts if w != name), key=lambda w: (len(w), w)
            )
            found[name] = {
                "name": name,
                "panel_name": "",  # 面板显示名，可在别名里选一个，留空则用 name
                "aliases": aliases,
                "plugin": plugin.name,
                "desc": "",
                "scope": list(SYNC_SCOPES),
                "block": False,
                "manual": False,
            }
    return list(found.values())


# ---------------------------------------------------------------- 合并
def _enforce_whitelist(cmd: Dict[str, Any], allow: set) -> None:
    """sync_plugins 白名单：非名单插件的指令一律 block（保留记录、不上传、面板里已存在的会被差删）

    allow 为空时不做任何强制，沿用命令条目的 block 手工字段。
    """
    if not allow:
        return
    plugin = str(cmd.get("plugin") or "")
    if not plugin or plugin not in allow:
        cmd["block"] = True


def load_merge_commands() -> List[Dict[str, Any]]:
    """自动扫描结果 ∪ 手工维护条目，得到最终指令清单并回写 commands.json

    规则:
    - 自动扫到的指令: 保留 JSON 里手工填过的 desc/scope/block，新增默认值。
    - JSON 中存在但没扫到的: 视为手工条目(manual=True)保留，不会被删除
      (用于纯关键词响应 / 不走 on_command 的指令)。
    - 重名时以已有 JSON 为准覆盖 auto 的 desc/scope/block。
    - 顶层 sync_plugins 白名单非空时，非名单插件的指令强制 block（见 _enforce_whitelist）。
    """
    doc = load_commands_doc()
    allow = set(str(x) for x in (doc.get("sync_plugins") or []))
    scopes = list(doc.get("scopes") or SYNC_SCOPES)
    prev = {c.get("name"): c for c in doc.get("commands", []) if c.get("name")}

    merged: List[Dict[str, Any]] = []
    for auto in collect_registered_commands():
        name = auto["name"]
        old = prev.pop(name, None)
        auto["desc"] = str((old or {}).get("desc") or "").strip()
        # 面板显示名（管理器里选的别名）必须保留，否则每次导出都会被重置回主指令名
        auto["panel_name"] = str((old or {}).get("panel_name") or "").strip()
        auto["scope"] = list((old or {}).get("scope") or scopes)
        auto["block"] = bool((old or {}).get("block"))
        _enforce_whitelist(auto, allow)
        merged.append(auto)

    # 纯手工条目（自动扫描覆盖不到的指令），原样保留
    for name, old in prev.items():
        keep = dict(old)
        keep.setdefault("aliases", [])
        keep.setdefault("scope", list(scopes))
        keep.setdefault("block", False)
        keep["manual"] = True
        _enforce_whitelist(keep, allow)
        merged.append(keep)

    merged.sort(key=lambda c: c["name"])
    doc["commands"] = merged
    save_commands_doc(doc)
    return merged


# ---------------------------------------------------------------- 平台面板数据
def _panel_id(panel: Any) -> Optional[str]:
    if isinstance(panel, dict):
        for key in ("panel_id", "command_panel_id", "id"):
            if panel.get(key):
                return str(panel[key])
    return None


def _panel_items_of(data: Any) -> List[Dict[str, Any]]:
    """从面板详情/PanelDetail 里取出 items（官方结构: data.panel.items）"""
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if isinstance(data, dict):
        inner = data.get("panel")
        if isinstance(inner, dict):
            items = inner.get("items")
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict)]
        items = data.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    return []


def _is_all_target(data: Any) -> bool:
    """target_type=all（或字段缺失）视为全量面板，specific 面板不接管"""
    if not isinstance(data, dict):
        return True
    tt = str(data.get("target_type", "") or "").lower()
    if tt and tt not in ("all", "global"):
        return False
    if data.get("user_openids") or data.get("group_openids"):
        return False
    return True


async def _list_panel_records(bot: Any, scope: str) -> List[Dict[str, Any]]:
    """分页拉全某 scope 的指令面板（兼容不同返回结构：records/panels/list）"""
    records: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        if cursor:
            resp = await get_command_panels(bot, scope, limit=_PAGE_LIMIT, cursor=cursor)
        else:
            resp = await get_command_panels(bot, scope, limit=_PAGE_LIMIT)
        if isinstance(resp, dict):
            raw = resp.get("records") or resp.get("panels") or resp.get("list") or []
            if isinstance(raw, dict):
                raw = raw.get("list") or raw.get("panels") or []
            nxt = resp.get("next_cursor")
            is_end = resp.get("is_end")
        elif isinstance(resp, list):
            raw, nxt, is_end = resp, None, True
        else:
            raw, nxt, is_end = [], None, True
        if isinstance(raw, list):
            records.extend(r for r in raw if isinstance(r, dict))
        if not nxt or is_end:
            break
        cursor = str(nxt)
        if len(records) > 500:  # 保险丝，避免死循环
            break
    return records


async def _list_all_panels(bot: Any, scope: str) -> List[Dict[str, Any]]:
    """把某 scope 的指令面板列表记录补全为详情（列表缺 panel/target_type 时补拉）"""
    panels: List[Dict[str, Any]] = []
    for rec in await _list_panel_records(bot, scope):
        pid = _panel_id(rec)
        if not pid:
            continue
        detail = rec
        if ("panel" not in rec and "items" not in rec) or "target_type" not in rec:
            try:
                got = await get_command_panel(bot, pid)
                if isinstance(got, dict) and got:
                    detail = {**rec, **got}
            except Exception:
                pass
        panels.append(detail)
    return panels


# ---------------------------------------------------------------- 差分同步
def _build_items(local: Sequence[Dict[str, Any]], scope: str) -> List[Dict[str, Any]]:
    """本地指令 → 面板元素（过滤屏蔽/超长名称，desc 截断到平台上限）"""
    items: List[Dict[str, Any]] = []
    for c in local:
        if c.get("block"):
            continue
        scopes = c.get("scope") or []
        if scope not in scopes:
            continue
        name = str(c.get("panel_name") or c.get("name") or "").strip()
        if not name:
            continue
        if _display_width(name) > _MAX_NAME_WIDTH:
            logger.warning(
                f"[指令同步] 指令「{name}」超出面板按钮长度限制({_MAX_NAME_WIDTH})，跳过上传"
            )
            continue
        items.append(
            {
                "type": "command",
                "name": name,
                "desc": _clip_display(c.get("desc") or "", _MAX_DESC_WIDTH),
            }
        )
    items.sort(key=lambda i: (len(i["name"]), i["name"]))
    return items


def _chunk(items: List[Dict[str, Any]], size: int = _MAX_ITEMS_PER_PANEL) -> List[List[Dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)] or []


async def sync_scope_panels(bot: Any, scope: str, local: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """差分同步单 scope 的 all 面板：多的删少的补（自动分面板/建面板）

    返回 {scope, panel_id, added[], removed[], panels}
    """
    chunks = _chunk(_build_items(local, scope))
    local_names = {i["name"] for chunk in chunks for i in chunk}

    # 该 scope 下所有 target=all 的面板视为“由本同步器维护”，逐面板对齐 chunk
    managed: List[Tuple[str, List[Dict[str, Any]]]] = []
    for panel in await _list_all_panels(bot, scope):
        if not _is_all_target(panel):
            continue
        pid = _panel_id(panel)
        if pid:
            managed.append((pid, _panel_items_of(panel)))

    remote_names = {
        str(i.get("name") or "") for _, old_items in managed for i in old_items if i.get("name")
    }
    added = sorted(local_names - remote_names)
    removed = sorted(remote_names - local_names)

    errors: List[str] = []

    # 1) 删掉多余的面板
    while len(managed) > len(chunks):
        pid, _old = managed.pop()
        try:
            await delete_command_panel(bot, pid)
            logger.info(f"[指令同步] {scope} 删除空余面板 {pid}")
        except Exception as e:
            msg = f"删除面板 {pid}: {e!r}"
            errors.append(msg)
            logger.warning(f"[指令同步] {scope} {msg}")

    # 2) 逐块对齐：少了创建，内容变了整面板覆盖
    created_pids: List[str] = []
    handled = 0  # 成功创建/覆盖/无需变更的块数
    for idx, chunk in enumerate(chunks):
        remark = f"指令同步 {scope} #{idx + 1}/{len(chunks)}"
        if idx < len(managed):
            pid, old_items = managed[idx]
            old_map = {(i.get("name"), i.get("desc"), i.get("type")) for i in old_items}
            new_map = {(i["name"], i["desc"], i["type"]) for i in chunk}
            if old_map == new_map and old_items:
                handled += 1
                continue
            try:
                await set_command_panel(bot, pid, chunk, remark=remark)
                handled += 1
                logger.info(f"[指令同步] {scope} 覆盖面板 {pid}")
            except Exception as e:
                msg = f"覆盖面板 {pid}: {e!r}"
                errors.append(msg)
                logger.warning(f"[指令同步] {scope} {msg}")
        else:
            try:
                created = await create_command_panel(bot, scope, chunk, remark=remark)
                pid = _panel_id(created) if isinstance(created, (dict, list)) else None
                created_pids.append(pid or str(created))
                handled += 1
                logger.info(f"[指令同步] {scope} 新建面板 {pid}")
            except Exception as e:
                msg = f"新建面板: {e!r}"
                errors.append(msg)
                logger.warning(f"[指令同步] {scope} {msg}")

    # 有需要推送的块却没全部成功 / 有多余面板没删掉 → 抛错，让外层不要误记为“已完成”
    if handled < len(chunks) or errors:
        raise RuntimeError(
            f"{scope} 面板同步 {handled}/{len(chunks)} 块成功"
            + (f"，未完成项：{'；'.join(errors)}" if errors else "")
        )

    primary = managed[0][0] if managed else (created_pids[0] if created_pids else "")
    return {
        "scope": scope,
        "panel_id": primary,
        "added": added,
        "removed": removed,
        "panels": len(managed) + len(created_pids),
    }


async def sync_all_goqbot_panels(bot: Any, scopes: Optional[Sequence[str]] = None) -> str:
    """goqbot 连接后：合并写 commands.json，并对各 scope 差分同步面板"""
    if not await is_goqbot(bot):
        raise PlatformNotSupportedError("仅 goqbot 实例支持同步指令面板")

    local = load_merge_commands()
    doc = load_commands_doc()
    scopes = list(scopes or doc.get("scopes") or SYNC_SCOPES)

    lines: List[str] = []
    failed: List[str] = []
    for scope in scopes:
        try:
            stat = await sync_scope_panels(bot, scope, local)
            if not stat["panel_id"] and not stat["added"]:
                lines.append(f"{scope}: 无指令，跳过")
                continue
            lines.append(
                f"{scope}: +{len(stat['added'])} -{len(stat['removed'])} "
                f"共{len([c for c in local if scope in (c.get('scope') or []) and not c.get('block')])} "
                f"指令 / {stat['panels']}面板"
            )
        except PlatformNotSupportedError:
            logger.warning(f"[指令同步] goqbot 不支持 scope={scope}，跳过")
        except Exception as e:
            failed.append(scope)
            logger.error(f"[指令同步] scope={scope} 同步失败: {e!r}")

    blocked = [c["name"] for c in local if c.get("block")]
    lines.append(f"已屏蔽 {len(blocked)} 条: {blocked}" if blocked else "无屏蔽指令")

    # 任一 scope 失败即抛错：cmd_sync 不会把 bot 记为“已同步”，下次连接会重试
    if failed:
        raise RuntimeError(f"指令面板同步失败（scope: {', '.join(failed)}）；其余: {'; '.join(lines)}")
    return "; ".join(lines)
