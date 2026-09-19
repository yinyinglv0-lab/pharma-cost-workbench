"""Staged cost imports. Confirmed revisions are immutable and published in one SQLite transaction."""
from __future__ import annotations
import hashlib
from contextlib import closing
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import pandas as pd
from paths import DATA_DIR, MANAGED_DIR
from enterprise.operations import guarded_write

IDENTITY = ['工厂','产品名称','产品规格','月份']
EXTRA = {'material':['原材料名称'], 'mfg':['费用类别']}
COST_NUM = ['产量(盒)','直接材料(元/盒)','直接人工(元/盒)','制造费用(元/盒)','单位成本(元/盒)','总成本(元)']
SCHEMA = {
 'cost': COST_NUM,
 'budget':['预算产量(盒)','预算直接材料(元/盒)','预算直接人工(元/盒)','预算制造费用(元/盒)','预算单位成本(元/盒)','预算总成本(元)'],
 'material':['产量(盒)','原材料名称','单位消耗成本(元/盒)','原材料总成本(元)','占总材料成本比例'],
 'labor':['产量(盒)','直接人工总额(元)','总工时(小时)','生产人数(人)','工作天数(天)'],
 'mfg':['产量(盒)','费用类别','单位费用(元/盒)','费用总额(元)'],
}

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def records(tables):
    return {key:json.loads(df.to_json(orient='records',force_ascii=False,double_precision=15)) for key,df in tables.items()}

def frames(value):
    return {key:pd.DataFrame(rows) for key,rows in value.items()}

def _now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def _key(table,row): return tuple(str(row.get(k,'')) for k in IDENTITY + EXTRA.get(table,[]))

def _values(row): return {k:v for k,v in row.items() if not k.startswith('_source')}

def _classify(columns):
    found=[k for k,cols in SCHEMA.items() if set(IDENTITY+cols).issubset(columns)]
    if len(found)!=1: raise ValueError('表头不能唯一识别；请使用提供的模板并保留工厂、产品名称、产品规格、月份及数值列')
    return found[0]


def validate(tables):
    errors,warnings=[],[]
    for key,rows in tables.items():
        seen=set()
        for row in rows:
            identity=_key(key,row)
            if identity in seen: errors.append(f'{key} 重复主键：{identity}')
            seen.add(identity)
            if key in ('cost26','cost25','erchang26','erchang25'):
                nums=[Decimal(str(row[c])) for c in COST_NUM]
                q,mat,lab,mfg,unit,amount=nums
                if abs(mat+lab+mfg-unit)>Decimal('.005'):errors.append(f'{identity} 三要素不等于单位成本')
                if abs(q*unit-amount)>Decimal('.01'):errors.append(f'{identity} 总成本不等于单位成本×产量')
    for row in tables.get('budget',[]):
        identity=_key('budget',row)
        q=Decimal(str(row['预算产量(盒)']));unit=Decimal(str(row['预算单位成本(元/盒)']))
        components=sum(Decimal(str(row[c])) for c in ('预算直接材料(元/盒)','预算直接人工(元/盒)','预算制造费用(元/盒)'))
        if abs(components-unit)>Decimal('.005'):errors.append(f'budget {identity} 预算三要素不等于预算单位成本')
        if abs(q*unit-Decimal(str(row['预算总成本(元)'])))>Decimal('.01'):errors.append(f'budget {identity} 预算总额不闭合')
    costs=[r for k in ('cost26','cost25','erchang26','erchang25') for r in tables.get(k,[])]
    index={_key('cost',r):r for r in costs}
    for table,amt,col in [('material','原材料总成本(元)','直接材料(元/盒)'),('mfg','费用总额(元)','制造费用(元/盒)'),('labor','直接人工总额(元)','直接人工(元/盒)')]:
        grouped={}
        for row in tables.get(table,[]):grouped.setdefault(_key('cost',row),[]).append(row)
        for identity,rows in grouped.items():
            base=index.get(identity)
            if not base:
                errors.append(f'{table} {identity} 缺少对应成本汇总');continue
            actual=sum(Decimal(str(r[amt])) for r in rows)
            target=Decimal(str(base[col]))*Decimal(str(base['产量(盒)']))
            if abs(actual-target)>Decimal('.01'):errors.append(f'{table} {identity} 明细总额{actual}与汇总{target}不闭合')
            for row in rows:
                q=Decimal(str(row['产量(盒)']))
                if q!=Decimal(str(base['产量(盒)'])):errors.append(f'{table} {identity} 明细产量与汇总不一致')
                unit_col={'material':'单位消耗成本(元/盒)','mfg':'单位费用(元/盒)'}.get(table)
                if unit_col and abs(Decimal(str(row[unit_col])) * q - Decimal(str(row[amt]))) > Decimal('.01'):
                    errors.append(f'{table} {identity} 明细行单位成本×产量与金额不闭合')
            if table=='material':
                try:
                    ratios=[Decimal(str(r['占总材料成本比例']).rstrip('%')) for r in rows]
                    if any(x<0 or x>100 for x in ratios) or abs(sum(ratios)-100)>Decimal('1'):
                        errors.append(f'material {identity} 材料比例合计须约为100%')
                except Exception:errors.append(f'material {identity} 材料比例格式错误')
        for row in tables.get('cost26',[]):
            if _key('cost',row) not in grouped:warnings.append(f"{row['产品名称']} {row['月份']} 缺少{table}明细，该项归因仅使用汇总")
    return errors,warnings


class CostRepository:
    def __init__(self,root=None,baseline_loader=None):
        self.root=Path(root) if root else MANAGED_DIR
        self.db=self.root/'cost_versions.db'
        self.baseline_loader=baseline_loader

    def _connect(self):
        self.root.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(self.db,timeout=15)
        con.row_factory=sqlite3.Row
        con.executescript('''
        CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, hash TEXT NOT NULL, data TEXT NOT NULL, changes TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stages(id TEXT PRIMARY KEY,created TEXT NOT NULL,actor TEXT NOT NULL,base_hash TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,action TEXT,actor TEXT,business_periods TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS source_files(hash TEXT PRIMARY KEY,name TEXT NOT NULL,content BLOB NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_revision_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT,'confirmed revisions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_revision_delete BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT,'confirmed revisions are immutable'); END;
        ''')
        return con

    def current(self):
        if self.db.exists():
            with closing(self._connect()) as con, con:
                row=con.execute('SELECT * FROM revisions ORDER BY id DESC LIMIT 1').fetchone()
                if row:
                    data = json.loads(row['data'])
                    if digest(data) != row['hash']:
                        raise ValueError('已确认成本数据摘要不匹配，禁止读取受损修订')
                    return {'revision':row['id'],'hash':row['hash'],'tables':data}
        if self.baseline_loader:tables=self.baseline_loader()
        else:
            from dashboard.data_layer import load_legacy_tables
            tables=load_legacy_tables()
        data=records(tables)
        return {'revision':0,'hash':digest(data),'tables':data}

    @guarded_write
    def stage(self,files,actor,*,principal=None):
        if not actor.strip():raise ValueError('请填写操作人')
        base=self.current()
        merged={k:list(rows) for k,rows in base['tables'].items()}
        changes=[]; errors=[]; seen=set(); originals=[]; counts={'新增':0,'修订':0,'重复':0}
        for filename,content in files:
            name=Path(filename.replace('\\','/')).name
            if not content or len(content)>20*1024*1024:raise ValueError('文件为空或超过20MB')
            sha=hashlib.sha256(content).hexdigest()
            originals.append((sha,name,content))
            ext=Path(name).suffix.lower()
            if ext=='.csv':
                try:df=pd.read_csv(io.BytesIO(content),encoding='utf-8-sig',dtype=str,keep_default_na=False)
                except UnicodeDecodeError:df=pd.read_csv(io.BytesIO(content),encoding='gb18030',dtype=str,keep_default_na=False)
                sheets={'CSV':df}
            elif ext=='.xlsx':
                import zipfile
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    members=archive.infolist()
                    if len(members)>10000 or sum(x.file_size for x in members)>100*1024*1024:
                        raise ValueError('XLSX解压后超过资源限制')
                with pd.ExcelFile(io.BytesIO(content)) as workbook:
                    if len(workbook.sheet_names)>50:raise ValueError('XLSX超过50个工作表')
                    sheets=pd.read_excel(workbook,sheet_name=None,dtype=str,keep_default_na=False)
            else:raise ValueError('成本数据支持CSV/XLSX；旧版XLS请另存为XLSX')
            for sheet,df in sheets.items():
                if df.empty:continue
                if len(df)>100000:raise ValueError('单表超过100000行，请按业务月份分批导入')
                df.columns=[str(x).strip() for x in df.columns]
                family=_classify(set(df.columns))
                required=IDENTITY+SCHEMA[family]
                for pos,(_,series) in enumerate(df[required].iterrows(),start=2):
                    row=series.to_dict()
                    try:
                        for field in IDENTITY+EXTRA.get(family,[]):
                            if pd.isna(row[field]) or not str(row[field]).strip():raise ValueError(f'{field}不能为空')
                            row[field]=str(row[field]).strip()
                        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',row['月份']):raise ValueError('月份须为YYYY-MM')
                        if row['工厂'] not in ('中药一厂','中药二厂'):raise ValueError('工厂未映射，请使用中药一厂/中药二厂')
                        if principal is not None:
                            from enterprise.security import require
                            require(principal, 'data.stage', factory=row['工厂'], product=row['产品名称'])
                        for field in SCHEMA[family]:
                            if field in ('原材料名称','费用类别','占总材料成本比例'):continue
                            num=Decimal(str(row[field]))
                            if not num.is_finite() or num<0:raise ValueError(f'{field}须为有限非负数')
                            if num > Decimal('1000000000000') or num.normalize().as_tuple().exponent < -6:
                                raise ValueError(f'{field}超出当前数据合同：数值上限1000000000000且最多六位有效小数')
                            if Decimal(str(float(num))) != num:
                                raise ValueError(f'{field}精度超出可无损读取范围，请按已批准的数据字典调整')
                            row[field]=float(num)
                        if family=='material':
                            ratio=str(row['占总材料成本比例']).strip()
                            if not ratio.endswith('%'):
                                value=Decimal(ratio)
                                if 0<=value<=1:value*=100
                                ratio=str(value)+'%'
                            row['占总材料成本比例']=ratio
                        table=family
                        if family=='cost':
                            table=('cost' if row['工厂']=='中药一厂' else 'erchang')+('25' if row['月份'][:4]<'2026' else '26')
                        ident=_key(table,row)
                        if (table,ident) in seen:raise ValueError('本批次主键重复；请先合并或去重')
                        seen.add((table,ident))
                        old=next((x for x in merged.get(table,[]) if _key(table,x)==ident),None)
                        if old and _values(old)==row:
                            counts['重复']+=1;continue
                        fields={k:{'before':old.get(k) if old else None,'after':v} for k,v in row.items() if not old or old.get(k)!=v}
                        original_periods=[r['月份'] for existing_table,rows in base['tables'].items() for r in rows
                                          if existing_table == table and all(str(r.get(k,'')) == row[k] for k in ('工厂','产品名称','产品规格'))]
                        is_historical = bool(original_periods and row['月份'] <= max(original_periods))
                        action='修订' if old or is_historical else '新增';counts[action]+=1
                        row.update({'_source_file':name,'_source_hash':sha,'_source_row':pos,'_source_sheet':sheet})
                        merged[table]=[x for x in merged.get(table,[]) if _key(table,x)!=ident]+[row]
                        changes.append({'action':action,'table':table,'key':dict(zip(IDENTITY+EXTRA.get(table,[]),ident)), 'fields':fields,'file':name,'source_hash':sha,'sheet':sheet,'row':pos})
                    except PermissionError:
                        raise
                    except Exception as exc:errors.append(f'{name}/{sheet} 记录{pos}: {exc}')
        if not seen:errors.append('没有识别到有效数据行')
        if not errors:
            validation,warnings=validate(merged);errors.extend(validation)
        else:warnings=[]
        stage_id=uuid.uuid4().hex
        periods=sorted({c['key']['月份'] for c in changes})
        payload={'stage_id':stage_id,'base_revision':base['revision'],'base_hash':base['hash'],'tables':merged,'counts':counts,'changes':changes,'errors':errors,'warnings':warnings,'business_periods':periods}
        with closing(self._connect()) as con, con:
            for sha,name,content in originals:con.execute('INSERT OR IGNORE INTO source_files VALUES(?,?,?)',(sha,name,content))
            con.execute('INSERT INTO stages VALUES(?,?,?,?,?,?)',(stage_id,_now(),actor,base['hash'],canonical(payload),'rejected' if errors else 'preview'))
            con.execute('INSERT INTO events(ts,action,actor,business_periods,detail) VALUES(?,?,?,?,?)',(_now(),'校验拒绝' if errors else '导入预览',actor,canonical(periods),canonical({'stage_id':stage_id,'counts':counts,'errors':errors})))
        return payload

    @guarded_write
    def commit(self,stage_id,actor,mode,reason=''):
        if not actor.strip():raise ValueError('操作人不能为空')
        # Legacy content may change outside the UI; optimistic locking checks the exact baseline.
        expected=self.current()['hash']
        con=self._connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('SELECT * FROM stages WHERE id=?',(stage_id,)).fetchone()
            if not row or row['status']!='preview':raise ValueError('预览不存在、已提交或校验未通过')
            p=json.loads(row['payload'])
            latest=con.execute('SELECT hash FROM revisions ORDER BY id DESC LIMIT 1').fetchone()
            current_hash=latest['hash'] if latest else expected
            if row['base_hash']!=current_hash:raise ValueError('数据已被其他操作更新，请重新预览')
            if p['counts']['修订'] and mode!='历史修订':raise ValueError('存在历史记录修改，必须选择历史修订')
            if p['counts']['修订'] and not reason.strip():raise ValueError('历史修订必须填写原因')
            if mode not in ('新月份新增','历史修订'):raise ValueError('请选择导入方式')
            if not p['changes']:
                con.execute('UPDATE stages SET status=? WHERE id=?',('duplicate',stage_id));con.commit()
                return {'revision':p['base_revision'],'duplicate':True}
            cur=con.execute('INSERT INTO revisions(created,actor,reason,hash,data,changes) VALUES(?,?,?,?,?,?)',(_now(),actor,reason or '新月份新增',digest(p['tables']),canonical(p['tables']),canonical(p['changes'])))
            revision=cur.lastrowid
            con.execute('UPDATE stages SET status=? WHERE id=?',('committed',stage_id))
            con.execute('INSERT INTO events(ts,action,actor,business_periods,detail) VALUES(?,?,?,?,?)',(_now(),mode,actor,canonical(p['business_periods']),canonical({'stage_id':stage_id,'revision':revision,'reason':reason,'counts':p['counts'],'changes':p['changes']})))
            con.commit();return {'revision':revision,'duplicate':False}
        except BaseException:
            con.rollback();raise
        finally:con.close()

    def history(self):
        if not self.db.exists():return []
        with closing(self._connect()) as con, con:return [dict(r) for r in con.execute('SELECT id,created,actor,reason,hash FROM revisions ORDER BY id DESC')]

    def events(self):
        if not self.db.exists():return []
        with closing(self._connect()) as con, con:
            rows=[dict(r) for r in con.execute('SELECT * FROM events ORDER BY id DESC')]
        for r in rows:
            r['business_periods']=json.loads(r['business_periods']);r['detail']=json.loads(r['detail'])
        return rows

    def original(self,sha):
        with closing(self._connect()) as con, con:
            row=con.execute('SELECT name,content FROM source_files WHERE hash=?',(sha,)).fetchone()
        if not row:raise ValueError('未找到原始文件')
        if hashlib.sha256(row['content']).hexdigest()!=sha:raise ValueError('原始文件哈希校验失败')
        return row['name'],row['content']


def active_tables(root=None):
    repo=CostRepository(root)
    if not repo.db.exists():return None
    with closing(repo._connect()) as con, con:
        row=con.execute('SELECT data,hash FROM revisions ORDER BY id DESC LIMIT 1').fetchone()
        if not row:
            return None
        data = json.loads(row['data'])
        if digest(data) != row['hash']:
            raise ValueError('已确认成本数据摘要不匹配，禁止读取受损修订')
        return frames(data)
