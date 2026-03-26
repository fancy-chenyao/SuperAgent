#!/usr/bin/env python
"""Test document generation with employee data."""

import asyncio
import httpx
import json


async def test_document_generation():
    """Test complete flow: HR query + document generation."""

    print("=" * 80)
    print("Testing Complete Flow: HR Query + Document Generation")
    print("=" * 80)

    # Step 1: Query employee info with salary
    print("\n[Step 1] Querying employee info...")
    hr_request = {
        "agent_name": "RemoteHRAssistantAgent",
        "messages": [
            {"type": "human", "content": "查询王强的完整信息包括工资"}
        ],
        "context": {
            "user_id": "test",
            "workflow_id": "test",
            "workflow_mode": "LAUNCH",
            "deep_thinking_mode": False,
            "debug": True
        },
        "tools": [
            {"name": "remote_person_info_tool", "description": "Query person info", "parameters": {}},
            {"name": "remote_salary_info_tool", "description": "Query salary info", "parameters": {}}
        ]
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post("http://127.0.0.1:8010/agent", json=hr_request)
        hr_result = response.json()

    if hr_result.get("status") != "success":
        print(f"  [FAIL] HR query failed: {hr_result.get('error')}")
        return

    # Find 王强
    wangqiang = None
    for record in hr_result.get("result", []):
        if record.get("adtEmpeNm") == "王强":
            wangqiang = record
            break

    if not wangqiang:
        print("  [FAIL] Cannot find 王强")
        return

    print(f"  [OK] Found 王强")
    print(f"    Name: {wangqiang.get('adtEmpeNm')}")
    print(f"    Position: {wangqiang.get('tcoPostNm')}")
    print(f"    Monthly Salary: {wangqiang.get('monthly_salary')}")
    print(f"    Annual Salary: {wangqiang.get('annual_salary')}")

    # Step 2: Generate document
    print("\n[Step 2] Generating income proof document...")

    # Build messages including HR result
    doc_messages = [
        {"type": "human", "content": "帮王强开买房用的个人收入证明"},
        {"type": "ai", "tool": "RemoteHRAssistantAgent", "content": json.dumps([wangqiang], ensure_ascii=False)}
    ]

    doc_request = {
        "agent_name": "RemoteDocumentGeneratorAgent",
        "messages": doc_messages,
        "context": {
            "user_id": "test",
            "workflow_id": "test",
            "workflow_mode": "LAUNCH",
            "deep_thinking_mode": False,
            "debug": True
        },
        "tools": [
            {"name": "remote_docx_generator_tool", "description": "Generate Word document", "parameters": {}}
        ]
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post("http://127.0.0.1:8010/agent", json=doc_request)
        doc_result = response.json()

    if doc_result.get("status") != "success":
        print(f"  [FAIL] Document generation failed: {doc_result.get('error')}")
        return

    result_data = doc_result.get("result", {})
    print(f"  [OK] Document generated")
    print(f"    File: {result_data.get('file_name')}")
    print(f"    Path: {result_data.get('file_path')}")
    print(f"    Template: {result_data.get('template_used')}")

    print("\n" + "=" * 80)
    print("SUCCESS! Complete flow working!")
    print("=" * 80)
    print("\nNext: Open the generated Word document to verify all fields are filled.")


if __name__ == "__main__":
    asyncio.run(test_document_generation())
