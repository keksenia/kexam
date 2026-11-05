"""Provider request boundaries, PNG identity and M14 wiring; no HTTP."""
from base64 import b64decode
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from app.call_guard import CallGuard, Policy, canonical, digest
from app.guard_demo import FixtureTransport, fixture_validator
from app.m05.l04 import CASES, build_l04_case
from app.m05.runtime import ROOT, freeze_identity
from app.provider_requests import build_hubris_request, RequestError
from app.prepare_l04 import prepare_experiment


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assets = []
        images = []
        for i in range(2):
            path = self.root / f"page-{i}.png"
            Image.new("RGB", (8+i, 8), (i*100, 20, 30)).save(path)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assets.append(dict(asset_id=f"p{i}", sha256=sha, path=str(path)))
            images.append(dict(id=f"p{i}", asset_id=f"p{i}", sha256=sha, role="task" if i==0 else "solution"))
        self.payload = dict(version="test-prompt", stage="read", package_sha256="a"*64,
            system="Literal system instructions", data=dict(images=images, context={},
                task=dict(statement=dict(completeness="confirmed", text="sin x = 0"))),
            response_schema=dict(type="object", properties=dict(pages=dict(type="array")), required=["pages"], additionalProperties=False))
        config = json.loads((ROOT/"planning/l04/configurations.json").read_text())
        self.profile = deepcopy(config["profiles"][0])
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start(); self.addCleanup(blocker.stop)

    def prepared(self, payload=None):
        payload = payload or self.payload
        return dict(payload=payload, payload_sha256=digest(payload), content_bytes=len(canonical(payload).encode()))

    def build(self, prepared=None, profile=None, assets=None, **kw):
        return build_hubris_request(prepared or self.prepared(), profile or self.profile,
            self.assets if assets is None else assets, asset_root=kw.get("asset_root", self.root))

    def test_api_body_uses_original_png_bytes_without_paths_or_metadata(self):
        api = self.build()
        body = json.loads(api.body_bytes)
        self.assertEqual(set(body), {"model", "messages", "stream", "max_tokens", "reasoning_effort", "response_format"})
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertTrue(body["messages"][0]["content"].startswith(self.payload["system"]+"\n\n"))
        self.assertEqual(body["response_format"], {"type":"json_object"})
        parts = body["messages"][1]["content"]
        self.assertEqual(json.loads(parts[0]["text"]), self.payload["data"])
        for index, asset in enumerate(self.assets):
            self.assertEqual(json.loads(parts[1+2*index]["text"])["asset_id"], asset["asset_id"])
            self.assertEqual(b64decode(parts[2+2*index]["image_url"]["url"].split(",",1)[1], validate=True), Path(asset["path"]).read_bytes())
        self.assertNotIn(str(self.root), api.body_bytes.decode())
        self.assertNotIn("package_sha256", body)
        self.assertEqual(api.body_sha256, hashlib.sha256(api.body_bytes).hexdigest())
        self.assertEqual(canonical(api.stage_request.payload).encode(), api.body_bytes)

    def test_changed_instruction_rejected_before_building(self):
        prepared = self.prepared(); prepared["payload"]["system"] += "changed"
        with self.assertRaisesRegex(RequestError,"content_drift"): self.build(prepared=prepared)

    def test_manifest_requires_every_image_in_exact_order(self):
        for assets in (self.assets[::-1], self.assets[:1], self.assets+self.assets[:1]):
            with self.subTest(count=len(assets)):
                with self.assertRaisesRegex(RequestError,"image_manifest_mismatch"): self.build(assets=assets)

    def test_replaced_image_not_silently_reencoded(self):
        Image.new("RGB",(8,8),"white").save(self.assets[0]["path"])
        with self.assertRaisesRegex(RequestError,"image_drift"): self.build()

    def test_external_path_or_symlink_cannot_enter_request(self):
        with self.assertRaisesRegex(RequestError,"image_path_invalid"): self.build(asset_root=self.root/"missing")

    def test_corrupt_png_not_accepted_even_with_updated_hash(self):
        path=Path(self.assets[0]["path"]); path.write_bytes(b"not a PNG")
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assets[0]["sha256"]=sha; self.payload["data"]["images"][0]["sha256"]=sha
        with self.assertRaisesRegex(RequestError,"image_invalid"): self.build()

    def test_model_settings_tools_caps_and_role_cannot_drift(self):
        changes=[dict(model="other"),dict(role="checker"),dict(sdk_automatic_retries=1),
            dict(requested_parameters={**self.profile["requested_parameters"],"max_tokens":6001}),
            dict(requested_parameters={**self.profile["requested_parameters"],"tools":[]}),
            dict(requested_parameters={**self.profile["requested_parameters"],"response_format":{"type":"json_schema"}})]
        for change in changes:
            with self.subTest(change=list(change)):
                with self.assertRaisesRegex(RequestError,"profile_invalid"): self.build(profile={**self.profile,**change})

    def test_body_limit_includes_base64_and_schema(self):
        with self.assertRaisesRegex(RequestError,"http_body_limit"):
            self.build(profile={**self.profile,"max_http_body_bytes":100})

    def test_read_cannot_receive_key_or_prepared_reading(self):
        for change in (dict(reference_key={}), dict(context={"units":[{"id":"u"}]})):
            payload=deepcopy(self.payload); payload["data"].update(change)
            with self.assertRaisesRegex(RequestError,"reading_reference_forbidden"):
                self.build(prepared=self.prepared(payload))

    def test_m14_receives_exact_body_and_deduplicates_it(self):
        api=self.build(); transport=FixtureTransport()
        policy=Policy(enabled=True,mode="fixture",approval_ref="SYNTHETIC-NO-MONEY",budget_micro_rub=150_000_000,
            max_attempts=2,max_total_calls=20,max_output_tokens=12000,max_response_bytes=512*1024,
            timeout_seconds=1,operation_timeout_seconds=5,expires_at=time.time()+60,
            ceilings={api.stage_request.fingerprint:dict(micro_rub=1_000_000,evidence_ref="synthetic")})
        guard=CallGuard(self.root/"ledger",policy)
        self.assertEqual(guard.execute(api.stage_request,transport,fixture_validator).status,"succeeded")
        self.assertTrue(guard.execute(api.stage_request,transport,fixture_validator).reused)
        self.assertEqual(transport.calls,1)
        saved=json.loads(next((self.root/"ledger/private_artifacts").glob("*-request.json")).read_text())
        self.assertEqual(canonical(saved["payload"]).encode(),api.body_bytes)

    def test_unconfirmed_condition_is_not_claimed_complete(self):
        self.payload["data"]["task"]["statement"]["completeness"]="unchecked"
        api=self.build()
        self.assertEqual(api.stage_request.validate(Policy(max_output_tokens=12000)),"incomplete_condition")


class PreparedExperimentTests(unittest.TestCase):
    def test_all_real_cases_have_paired_content_and_images(self):
        config=json.loads((ROOT/"planning/l04/configurations.json").read_text())
        profiles={p["id"]:p for p in config["profiles"]}
        before=freeze_identity(); fingerprints=[]
        with patch("socket.socket.connect",side_effect=AssertionError("Network forbidden")):
            for case_id in CASES:
                case=build_l04_case(case_id)
                for stage,suffix in (("reading","ocr"),("assessment","checker")):
                    pair=[]
                    for model in ("G","D"):
                        api=build_hubris_request(case[stage],profiles[model+"-"+suffix],case["assets"],
                            asset_root=ROOT/"data_local/l03/model_inputs"/case_id)
                        self.assertIsNone(api.stage_request.validate(Policy(max_output_tokens=12000)))
                        fingerprints.append(api.stage_request.fingerprint)
                        pair.append(api.stage_request.payload["messages"])
                    self.assertEqual(pair[0],pair[1])
                    self.assertEqual(len(pair[0][1]["content"]),7 if case_id==CASES[-1] else 5)
        self.assertEqual(len(set(fingerprints)),16)
        self.assertEqual(freeze_identity(),before)

    def test_preparation_repeat_does_not_overwrite_and_budget_matches(self):
        # Stored bodies are versioned and private, never model input in planning/.
        result=prepare_experiment()
        self.assertEqual((result["primary_calls"],result["technical_retry_reserve"],result["max_total_calls"],result["budget_rub"]),(16,4,20,200))
        self.assertEqual(result["policy_template"]["budget_micro_rub"], 200_000_000)
        self.assertFalse(result["launch_ready"])
        self.assertEqual(result["policy_template"]["ceilings"],{})
        self.assertFalse(result["policy_template"]["enabled"])
        self.assertEqual(result["network_model_calls"],0)
        self.assertEqual(result,prepare_experiment())

    def test_private_output_required(self):
        with self.assertRaisesRegex(ValueError,"private_output_required"):
            prepare_experiment(ROOT/"planning/public-requests")
