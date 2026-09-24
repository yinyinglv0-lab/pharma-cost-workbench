# -*- coding: utf-8 -*-
"""模型稳定性基线重测脚本（五次固定批次，绝不自动触发付费调用）。

用法：
    python scripts/stability_runner.py --dry-run        # 只做环境/配置就绪检查，零调用
    python scripts/stability_runner.py --run --runs 5   # 显式授权后执行五次真实调用
                                                         # 每次生成独立回执，失败批次保留

纪律（沿用系统既定规则）：
- 默认零付费调用；只有显式 --run 才发起请求；
- 每次运行一个独立批次目录，逐次落回执（request hash / 响应 / 采用状态 / 延迟 / token）；
- 任一批次失败不重试、不拼通过率；五次中任何一次不达标 → 整体 0/N 或 N/N 如实记录；
- 换模型必须重新跑本脚本，历史批次不得继承。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _now():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def _receipt_dir():
    path = ROOT / 'artifacts' / 'stability_runs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def dry_run():
    from enterprise.model_registry import selection_state
    from enterprise.security import credential_status
    state = selection_state()
    rows = state['entries']
    selected = next((row for row in rows if row.get('registry_id') != 'legacy'
                     and row.get('selectable')), None)
    legacy = next((row for row in rows if row.get('registry_id') == 'legacy'), None)
    print('就绪检查（零模型调用）:')
    for row in rows:
        print(f"  {row['registry_id']:12s} model={row['model']:18s} "
              f"configured={row['configured']} approved={row['approved_cloud']} "
              f"selectable={row['selectable']} timeout={row['timeout_seconds']}s")
    print(f"  当前已选可用的注册模型: {selected['registry_id'] if selected else '无（仅 legacy）'}")
    if not selected and (not legacy or not legacy['selectable']):
        print('  ⚠️ 没有可选模型：请先在「模型配置」页填写密钥与授权，或配置 *_API_KEY 与 *_APPROVED_CLOUD=true 环境变量')
        return 1
    print('  凭证状态: ' + json.dumps(credential_status(), ensure_ascii=False))
    return 0


def run_batch(runs: int, product: str = '银黄口服液', month: str = '2026-05'):
    from attribution_gen import generate_attribution
    batch = _now()
    batch_dir = _receipt_dir() / f'batch_{batch}'
    batch_dir.mkdir(parents=True, exist_ok=True)
    summary = {'batch': batch, 'runs': runs, 'results': [], 'stable': None}
    for index in range(1, runs + 1):
        started = time.monotonic()
        receipt = {'run': index, 'started_at': _now(), 'product': product,
                   'month': month, 'ok': None, 'latency_seconds': None,
                   'generation_status': None, 'used_llm': None}
        try:
            result = generate_attribution(product, month, use_llm=True,
                                          require_hybrid=True)
            receipt['latency_seconds'] = round(time.monotonic() - started, 2)
            receipt['used_llm'] = bool(result.get('used_llm'))
            receipt['generation_status'] = result.get('generation_status')
            receipt['input_data_hash'] = result.get('input_data_hash')
            receipt['ok'] = bool(result.get('used_llm'))
        except Exception as exc:
            receipt['latency_seconds'] = round(time.monotonic() - started, 2)
            receipt['ok'] = False
            receipt['error'] = f'{type(exc).__name__}: {exc}'
        summary['results'].append(receipt)
        print(f"  第 {index}/{runs} 次: ok={receipt['ok']} "
              f"status={receipt.get('generation_status')} "
              f"latency={receipt.get('latency_seconds')}s")
    summary['stable'] = all(item['ok'] for item in summary['results'])
    receipt_path = batch_dir / 'summary.json'
    receipt_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding='utf-8')
    print(f"\n结论: {'✅ 5/5 稳定' if summary['stable'] else '❌ 未达 5/5（失败批次已保留）'}")
    print(f"回执: {receipt_path}")
    return 0 if summary['stable'] else 1


def main():
    ap = argparse.ArgumentParser(description='模型稳定性基线重测（默认零付费调用）')
    ap.add_argument('--dry-run', action='store_true', help='只做就绪检查，零调用')
    ap.add_argument('--run', action='store_true', help='显式授权发起真实模型调用')
    ap.add_argument('--runs', type=int, default=5)
    ap.add_argument('--product', default='银黄口服液')
    ap.add_argument('--month', default='2026-05')
    args = ap.parse_args()
    if args.run:
        if not 1 <= args.runs <= 10:
            print('--runs 须在 1-10 之间')
            return 2
        print(f'⚠️ 将发起 {args.runs} 次真实付费模型调用（产品 {args.product}，月份 {args.month}）')
        return run_batch(args.runs, args.product, args.month)
    return dry_run()


if __name__ == '__main__':
    sys.exit(main())
#（注：内容由AI生成）
