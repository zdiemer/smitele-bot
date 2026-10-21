"""Failed refreshes preserve the disk cache and the published catalogue."""
import json
from types import SimpleNamespace

import pytest
from smite2.provider import Smite2Provider
from smite2.wiki_client import WikiClient
from smite2 import provider as module


def key(action):
    return json.dumps(dict(format='json', formatversion='2', maxlag=5, action=action), sort_keys=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['exception', 'empty', 'success'])
async def test_bad_refresh_uses_last_valid_catalogue(tmp_path, monkeypatch, failure):
    cache=tmp_path/'wiki_cache.json'
    original=json.dumps({key('gods'):{'values':{'old-god':1}}, key('items'):{'values':{'old-item':1}}})
    cache.write_text(original)
    version=tmp_path/'version';version.write_text('old-revision')
    monkeypatch.setattr(Smite2Provider,'cache_path',property(lambda self:str(cache)))
    monkeypatch.setattr(Smite2Provider,'version_path',property(lambda self:str(version)))
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def json(self):
            if self.action=='items':
                if failure=='exception':raise ValueError('invalid wiki response')
                return {'values':{'new-item':1} if failure=='success' else {}}
            return {'values':{'new-god':1}}
    class Session:
        def get(self,url,params):
            r=Response();r.action=params['action'];return r
    monkeypatch.setattr(Smite2Provider,'_Smite2Provider__client',lambda self:WikiClient(cache_path=str(cache),session=Session()))
    async def revision(self, client):return 'new-revision'
    monkeypatch.setattr(Smite2Provider,'_Smite2Provider__revision_hash',revision)
    async def gods(client, **kwargs):return (await client.get(action='gods'))['values'], 'god-index', 'skins'
    async def items(client, **kwargs):return (await client.get(action='items'))['values'], 'item-index'
    monkeypatch.setattr(module.gods_module,'load',gods)
    monkeypatch.setattr(module.items_module,'load',items)
    provider=Smite2Provider(silent=False)
    await provider.create()
    if failure=='success':
        assert provider.gods=={'new-god':1}
        assert provider.items=={'new-item':1}
        assert json.loads(cache.read_text())[key('items')]=={'values':{'new-item':1}}
        assert version.read_text()=='new-revision'
        return
    assert provider.gods=={'old-god':1}
    assert provider.items=={'old-item':1}
    assert cache.read_text()==original
    assert version.read_text()=='old-revision'


@pytest.mark.asyncio
async def test_cancelled_refresh_does_not_flush_cleared_cache(tmp_path):
    import asyncio
    cache=tmp_path/'cache.json';cache.write_text('{"last-good": {}}')
    with pytest.raises(asyncio.CancelledError):
        async with WikiClient(cache_path=str(cache),session=SimpleNamespace()) as client:
            client.clear_cache()
            raise asyncio.CancelledError()
    assert json.loads(cache.read_text())=={'last-good':{}}
