"""Saved analysis inputs and outputs; replay never invokes a model or reads current data."""
import base64
from contextlib import closing
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from paths import DATA_DIR, BASE_DIR, MANAGED_DIR
from enterprise.operations import guarded_write
from enterprise.cost_imports import canonical, records

CODE_FILES = ('attribution_gen.py','attribution_facts.py','attribution_decomposition.py','attribution_narrative.py','dashboard/data_layer.py')


def current_provenance():
    market=DATA_DIR/'药材市场价格行情_2026年上半年.csv'
    return {'code_hashes':{name:hashlib.sha256((BASE_DIR/name).read_bytes()).hexdigest() for name in CODE_FILES},
            'market_sha256':hashlib.sha256(market.read_bytes()).hexdigest() if market.is_file() else None}


class SnapshotRepository:
    def __init__(self,root=None,principal=None):
        self.root=Path(root) if root else MANAGED_DIR
        self.db=self.root/'snapshots.db'
        self.principal=principal

    def _require(self, action, product=None):
        if self.principal is not None:
            from enterprise.security import require
            require(self.principal, action, factory='中药一厂', product=product)

    def _public_payload(self, payload):
        if self.principal is None:
            return payload
        from enterprise.security import filter_tables
        from enterprise.cost_imports import frames, records
        payload['tables'] = records(filter_tables(self.principal, frames(payload.get('tables', {})), action='report.read'))
        payload['access_view'] = '按当前账户范围筛选的回看副本；归档原件保持不变'
        # Source CSV workbooks may contain other products. Preserve the archive,
        # but never expose unscoped original bytes through a scoped snapshot.
        if '*' not in self.principal.products or '*' not in self.principal.factories:
            for source in payload.get('source_files', []):
                source.pop('base64', None)
                source['original_access'] = '原件包含其他范围，须由具备全部原件范围权限的审核者获取'
        from enterprise.knowledge import Repository
        knowledge=Repository(self.root, principal=self.principal)
        for source in payload.get('knowledge_documents', []):
            document=knowledge.get(version_id=source['version']['version_id'])
            if not document:
                raise PermissionError('快照包含当前账户无权访问的知识版本；禁止展示或导出')
        # Frozen analysis prose may itself quote the source, so redacting just
        # attachment bytes cannot safely implement a later permission revocation.
        for source in payload.get('analysis', {}).get('sources', []):
            if source.get('version_id') and not knowledge.get(version_id=source['version_id']):
                raise PermissionError('快照引用权限已变化，请联系授权审核者')
        return payload

    def _connect(self):
        self.root.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(self.db)
        con.row_factory=sqlite3.Row
        con.execute('CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,created TEXT,product TEXT,month TEXT,actor TEXT,hash TEXT,payload TEXT)')
        return con

    @guarded_write
    def save(self,result,tables,actor,versions=None):
        if not actor.strip():raise ValueError('请填写操作人')
        p=result['payload']
        self._require('analysis.generate', p['product'])
        if self.principal is not None:
            actor=self.principal.user_id
        provenance=result.get('input_provenance')
        from enterprise.cost_imports import digest
        if result.get('input_data_hash') != digest(records(tables)):
            raise ValueError('当前成本输入与生成时不一致，请重新分析后保存')
        if provenance != current_provenance():
            raise ValueError('分析后市场资料或计算版本已变化，请重新生成再保存快照')
        frozen={'analysis':result,'tables':records(tables),'versions':versions or {},'created':datetime.now(timezone.utc).isoformat(), 'review_status':'needs_review','code_hashes':{},'source_files':[]}
        frozen['code_hashes']=provenance['code_hashes']
        # Preserve original local cost/market bytes, but never traverse outside the project or read secrets.
        import pandas as pd
        references={(str(r['_source_file']),str(r['_source_hash']) if pd.notna(r.get('_source_hash')) else '')
                    for df in tables.values() if '_source_file' in df for r in df.to_dict('records')
                    if pd.notna(r.get('_source_file')) and r.get('_source_file')}
        if provenance['market_sha256']:
            references.add(('药材市场价格行情_2026年上半年.csv',provenance['market_sha256']))
        for name,expected in sorted(references):
            path=Path(name)
            if not path.is_absolute():path=DATA_DIR/path
            path=path.resolve()
            raw=None
            if path.is_relative_to(DATA_DIR) and path.suffix.lower() in ('.csv','.xlsx') and path.is_file():
                candidate=path.read_bytes()
                if not expected or hashlib.sha256(candidate).hexdigest()==expected:raw=candidate
            if raw is None and expected:
                from enterprise.cost_imports import CostRepository
                repo=CostRepository(self.root)
                if repo.db.exists():
                    try:
                        _,raw=repo.original(expected)
                    except ValueError:pass
            if raw is None and expected:
                raise ValueError(f'原始资料{name}已变化或不可读取，请重新生成或恢复原件后保存')
            if raw is not None:
                frozen['source_files'].append({'file':path.name,'sha256':hashlib.sha256(raw).hexdigest(),'base64':base64.b64encode(raw).decode('ascii')})
        frozen['knowledge_documents']=[]
        from enterprise.knowledge import Repository
        knowledge=Repository(self.root)
        for source in result.get('sources',[]):
            if source.get('version_id'):
                document=knowledge.get(version_id=source['version_id'])
                if document:
                    blob=knowledge.read_blob(document['sha256'])
                    frozen['knowledge_documents'].append({'version':document,'base64':base64.b64encode(blob).decode('ascii')})
        content=canonical(frozen)
        sha=hashlib.sha256(content.encode()).hexdigest()
        ident=uuid.uuid4().hex
        with closing(self._connect()) as con, con:
            con.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',(ident,frozen['created'],p['product'],p['month'],actor,sha,content))
        return ident

    def list(self):
        self._require('report.read')
        if not self.db.exists():return []
        with closing(self._connect()) as con, con:
            rows=[dict(r) for r in con.execute('SELECT id,created,product,month,actor,hash FROM snapshots ORDER BY created DESC')]
        if self.principal is None:
            return rows
        from enterprise.security import can
        return [r for r in rows if can(self.principal, 'report.read', factory='中药一厂', product=r['product'])]

    def get(self,ident):
        with closing(self._connect()) as con, con:row=con.execute('SELECT * FROM snapshots WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('快照不存在')
        self._require('report.read', row['product'])
        if hashlib.sha256(row['payload'].encode()).hexdigest()!=row['hash']:raise ValueError('快照内容校验失败，禁止展示修改后的历史结果')
        return self._public_payload(json.loads(row['payload']))
