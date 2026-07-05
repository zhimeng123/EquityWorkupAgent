import httpx

from mlc_agent.official_site import fetch_official_profile


def test_discovers_company_profile_page():
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<html><a href="/about">公司简介</a><p>首页普通内容不足以作为介绍。</p></html>',
                request=request,
            )
        return httpx.Response(
            200,
            text="<html><main><p>" + "紫光股份专注于信息通信基础设施和数字化解决方案。" * 12 + "</p></main></html>",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        page = fetch_official_profile(client, "https://company.example/")
    assert page.url == "https://company.example/about"
    assert "数字化解决方案" in page.text

