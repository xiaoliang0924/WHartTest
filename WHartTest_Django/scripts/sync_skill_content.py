"""把运行时 SKILL.md 同步进 skills_skill.skill_content。

背景：Agent 通过 read_skill_content 工具读取的是 DB 的 skill_content 字段，
而不是磁盘上的 SKILL.md。历史上两者已漂移（DB 2601 字符 / 磁盘 3498 字符 / 源仓库 4278 字符），
导致写进磁盘的新指引对 Agent 完全不可见。
"""
import os
import sys

sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wharttest_django.settings')

import django  # noqa: E402

django.setup()

from skills.models import Skill  # noqa: E402

DRY_RUN = os.environ.get('SYNC_DRY_RUN') == '1'


def sync(skill_id: int, skill_md_path: str) -> None:
    with open(skill_md_path, 'r', encoding='utf-8') as f:
        content = f.read()
    skill = Skill.objects.filter(id=skill_id).first()
    if skill is None:
        print(f'[SKIP] skill id={skill_id} 不存在')
        return
    old = skill.skill_content or ''
    print(f'[INFO] skill id={skill_id} name={skill.name}')
    print(f'       旧内容 {len(old)} 字符 -> 新内容 {len(content)} 字符')
    if old.strip() == content.strip():
        print('       已一致，无需更新')
        return
    if DRY_RUN:
        print('       DRY_RUN=1，仅打印不写库')
        return
    skill.skill_content = content
    skill.save(update_fields=['skill_content'])
    skill.refresh_from_db()
    print(f'       写入完成，复核长度 = {len(skill.skill_content or "")}')


def main() -> None:
    sync(20, '/app/data/media/skills/1/20/SKILL.md')


if __name__ == '__main__':
    main()
