"""Sanitized live shape fixtures from the new OVH deployment, 2026-11-17.

Only official model identifiers and contract field shapes are retained. No
credentials, account identity, prompts, endpoint response bodies or user data.
"""
import asyncio
import pytest

from app.adapters import Raccoon,WPS,CodeArts,WorkBuddyInternational
from app.management import catalog_models
from app.model_catalog import normalize
from app.upstream import UpstreamError


FIXTURES={
    "小浣熊":{"categories":[{"type":"chat","default_model":"raccoon-090dd3","models":[{"name":name,"model_name":"display-name","visible":False,"params":{"context_window":1,"max_tokens":1}} for name in ["raccoon-090dd3","raccoon-19b265","raccoon-405a1c","sn-deepseek-v4-1-flash-1017","sn-glm-5-3","sn-glm-5-3-flash","sn-kimi-k3","sn-qwen4-max","sn-sensenova-6-8-flash","sn-sensenova-6-8-flash-lite"]]}]},
    "WPS 灵犀":{"plans":[{"key":"plan_max","name":"display-name"},{"key":"plan_pro","name":"display-name"}],"models":[{"key":name,"name":"display-name","support_multimodal":False} for name in ["GLM-5.3","GLM-5.3-Flash","deepseek-v4-pro-0813","deepseek-v4.1-flash","doubao_seed_2_1_pro_260628","kimi-k3","kimi-k3.1-pro","mimo-v2.6-pro","minimax_m3","qwen3.8_flash","qwen3.8_max"]]},
    "CodeArts":{"count":12,"builtinModels":[{"model_id":name,"model_name":"display-name","enable":True,"display_enabled":False,"supports_images":False} for name in ["GLM-5.2","deepseek-v4.1-flash","deepseek-v4.1-flash-1017","glm-5.2-sft-harmony","glm-5.3","glm-5.3-flash","kimi-k3","kimi-k3.1-pro","openpangu-2.0-flash","openpangu-2.0-pro","openpangu-2.1-pro","speed"]]},
    "WorkBuddy 国际":{"models":[{"id":name,"name":"display-name","supportsToolCall":True} for name in ["deepseek-v4.1-flash-sg","default-model","gemini-3.8-flash","gemini-4-argon","gemini-4-carbon","glm-5.3","glm-5.3-flash","gpt-5.6-luna","gpt-5.6-sol","gpt-5.6-terra","gpt-6-astra","gpt-6-luna","gpt-6-sol","gpt-6.1-sol","grok-4.7","hy-image-3.5","hy4flash","hy4flash-free","hy4std","kimi-k3","kimi-k3.1-pro","kimi-k3.1-pro-highspeed","kling-video-v3","max-model","minimax-m3.1-flash"]]},
}


@pytest.mark.parametrize("platform,count",[("小浣熊",10),("WPS 灵犀",13),("CodeArts",12),("WorkBuddy 国际",23)])
def test_observed_catalog_shapes_preserve_wire_ids(platform,count):
    names=catalog_models(normalize(platform,FIXTURES[platform]))
    assert len(names)==count and "display-name" not in names
    if platform=="WPS 灵犀":assert {"plan_pro","plan_max","deepseek-v4.1-flash"}.issubset(names)
    if platform=="WorkBuddy 国际":assert "kling-video-v3" not in names and "hy-image-3.5" not in names


@pytest.mark.parametrize("platform",list(FIXTURES))
def test_unknown_catalog_cannot_be_reported_as_success(platform):
    with pytest.raises(UpstreamError):normalize(platform,{"message":"unverified"})


def test_adapter_methods_use_real_catalog_contracts():
    async def run():
        for cls,platform in [(Raccoon,"小浣熊"),(WPS,"WPS 灵犀"),(CodeArts,"CodeArts"),(WorkBuddyInternational,"WorkBuddy 国际")]:
            adapter=cls(None,{},"synthetic")
            paths=[]
            async def request(method,url,headers):paths.append(url);return FIXTURES[platform]
            async def read(url):paths.append(url);return FIXTURES[platform]
            adapter.request=request;adapter.read=read;adapter.headers=lambda:{}
            assert catalog_models(await adapter.models())==catalog_models(normalize(platform,FIXTURES[platform]))
            if platform=="WorkBuddy 国际":assert paths==["https://www.workbuddy.ai/v3/config"]
    asyncio.run(run())
