import asyncio
import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/smite2_collector'))
from catalogue import Catalogue, load, page_rows
from smite2.ids import god_id, item_id
from source_rows import normalize_source

FIXTURES=Path(__file__).parent/'fixtures/smite2_sources'


def data():return json.loads((FIXTURES/'catalogue.json').read_text())


def test_ids_and_recorded_scoreboard():
    p=Catalogue(data())
    assert p.god_id_from_name('Hua Mulan')==god_id('mulan')
    assert p.god_id_from_name('bari')==god_id('bari')
    assert p.god_id_from_name('Gods.JingWei')==god_id('jing-wei')
    assert p.item_by_name("Chronos' Pendant").id==item_id('chronos-pendant')
    _,rows=normalize_source(json.loads((FIXTURES/'smitesource_full.json').read_text()),json.loads((FIXTURES/'smitesource_assets.json').read_text()),p)
    assert len(rows)==6 and sum(r['UnknownItems'] for r in rows)==0


def test_page_chunks_are_data_not_code():
    payload='12:["$","client",null,{"catalog":'+json.dumps(data()['gods'])+'}]'
    chunks=[payload[:75],payload[75:]]
    html=''.join('<script>self.__next_f.push('+json.dumps([1,c])+')</script>' for c in chunks)
    assert page_rows(html,'catalog')==data()['gods']
    with pytest.raises(ValueError):page_rows('<html>challenge</html>','catalog')


@pytest.mark.asyncio
async def test_primary_then_offline_snapshot(tmp_path):
    path=tmp_path/'catalogue.json'
    async def good():return data()
    def no_wiki():raise AssertionError('wiki must not be requested')
    _,origin=await load(path,good,no_wiki)
    assert origin=='SmiteSource'
    saved=path.read_bytes()
    async def offline():raise TimeoutError()
    p,origin=await load(path,offline,no_wiki)
    assert origin=='SmiteSource-cache' and len(p.gods)==94
    assert path.read_bytes()==saved


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['empty','duplicate','shrink','cancel'])
async def test_invalid_refresh_preserves_cache(tmp_path,mode):
    path=tmp_path/'catalogue.json';path.write_text(json.dumps(data()));saved=path.read_bytes()
    async def bad():
        d=data()
        if mode=='cancel':raise asyncio.CancelledError()
        if mode=='empty':d['items']=[]
        if mode=='duplicate':d['items'].append(d['items'][0])
        if mode=='shrink':d['items']=d['items'][:110]
        return d
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):await load(path,bad)
    else:
        _,origin=await load(path,bad)
        assert origin=='SmiteSource-cache'
    assert path.read_bytes()==saved


@pytest.mark.asyncio
async def test_cold_start_wiki_fallback(tmp_path):
    class Wiki:
        gods={'god':1};items={'item':1}
        async def create(self):pass
    async def offline():raise TimeoutError()
    p,origin=await load(tmp_path/'missing',offline,Wiki)
    assert isinstance(p,Wiki) and origin=='wiki'
