from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers,QueryParams
from gsm_api.application_proxy import http_proxy


@pytest.mark.asyncio
async def test_proxy_never_forwards_browser_secrets_or_upstream_cookies(monkeypatch):
    captured=[]
    def handle(request):
        captured.append(request)
        return httpx.Response(200,headers={'Content-Type':'text/html','Set-Cookie':'gsm_session=attack; Path=/',
            'Access-Control-Allow-Origin':'*','Service-Worker-Allowed':'/'},content=b'notebook')
    real=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handle),**kw))
    async def body():
        yield b'{}'
    request=SimpleNamespace(method='POST',headers=Headers({'Cookie':'gsm_session=manager; gsm_app=scoped',
        'Authorization':'Bearer browser-secret','X-CSRF-Token':'manager-csrf','Content-Type':'application/json'}),
        query_params=QueryParams(),stream=body)
    response=await http_proxy(request,'http://private-app/path',{'Authorization':'token internal-app-only'})
    assert captured[0].headers['authorization']=='token internal-app-only'
    assert 'cookie' not in captured[0].headers and 'x-csrf-token' not in captured[0].headers
    assert 'set-cookie' not in response.headers and 'access-control-allow-origin' not in response.headers
    assert response.headers['service-worker-allowed']=='/__disabled__'
    await response.background()


@pytest.mark.asyncio
async def test_application_upload_is_bounded():
    with pytest.raises(HTTPException) as error:
        await http_proxy(SimpleNamespace(headers=Headers({'Content-Length':str(101*1024*1024)})), 'http://private-app/path',{})
    assert error.value.status_code==413
