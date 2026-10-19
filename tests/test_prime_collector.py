import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'/'smite2_collector'))
from prime_rows import normalize,InvalidMatch
from collect_prime import persist
from store import SeenMatches,output_path
from smite2.ids import item_id,slugify,s2_id
import match_storage

@pytest.fixture
def report():
    return json.loads((Path(__file__).parent/'fixtures/smite2_sources/smiteprime_match.json').read_text())

class Catalogue:
    def god_id_from_name(self,name):return s2_id('god',slugify(name))
    def item_by_name(self,name):return SimpleNamespace(id=item_id(slugify(name)))


def test_recorded_complete_match_preserves_slots_and_teams(report):
    date,rows=normalize(report,Catalogue())
    assert date=='2026-10-18'
    assert len(rows)==6
    assert sum(r['Win_Status']=='Winner' for r in rows)==3
    assert rows[0]['Kills_Player']==13
    assert rows[0]['ItemId1']==item_id('jotunns-revenge')
    assert rows[0]['ItemId6']==item_id('lucerne-hammer')
    assert rows[0]['ItemId1']!=report['players'][0]['items']['item_1']['item_id']
    assert rows[0]['Source']=='SmitePrime'
    assert rows[0]['GodId']>2**30


def test_unknown_core_item_remains_zero_in_its_slot(report):
    class Partial(Catalogue):
        def item_by_name(self,name):
            return None if name=="Jotunn's Revenge" else super().item_by_name(name)
    _,rows=normalize(report,Partial())
    assert rows[0]['ItemId1']==0
    assert rows[0]['ItemId2']==item_id('barbed-carver')
    assert rows[0]['UnknownItems']==1


@pytest.mark.parametrize('change',[lambda r:r.update(state='live'),lambda r:r['players'].pop(),lambda r:r.update(winning_team=None),lambda r:r['players'][0].update(kills=None)])
def test_incomplete_reports_never_produce_corpus_rows(report,change):
    change(report)
    with pytest.raises(InvalidMatch):normalize(report,Catalogue())


def test_unmapped_aspect_is_not_misreported_as_no_aspect(report):
    class Partial(Catalogue):
        def item_by_name(self,name):return None
    with pytest.raises(InvalidMatch,match='unknown aspect'):normalize(report,Partial())


def test_durable_append_and_recovery_do_not_duplicate_match(tmp_path,report):
    date,rows=normalize(report,Catalogue())
    corpus=str(tmp_path/'corpus');state=str(tmp_path/'state')
    seen=SeenMatches(state)
    assert persist(corpus,seen,date,rows)==6
    assert rows[0]['Match'] in SeenMatches(state)
    assert persist(corpus,seen,date,rows)==0
    # Simulate interruption after append but before updating the index.
    recovered=SeenMatches(str(tmp_path/'missing-index'))
    assert persist(corpus,recovered,date,rows)==0
    assert rows[0]['Match'] in recovered
    assert len(match_storage.read_frame(output_path(corpus,date)))==6


def test_explicit_aspect_names_use_existing_slug_ids(report):
    class Partial(Catalogue):
        def item_by_name(self,name):
            return None if name.startswith('Aspect of ') else super().item_by_name(name)
    _,rows=normalize(report,Partial())
    assert rows[-1]['Aspect']==item_id('aspect-of-the-thickbark')
