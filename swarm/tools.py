"""
Async tool wrappers and OpenAI‑style tool schemas.

Every function returns a string summarising the result of the operation so
the next model turn can reason about what happened.  Schema dicts provided at
module level follow the standard ``openai.tool_use`` format (function
calling) — works identically with Azure or local Ollama back‑ends.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3 as _sqlite
import time
import urllib
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Minimal schema factory                                                      #
# --------------------------------------------------------------------------- #

def _tool_schema(
    name: str,
    description: str,
    parameters: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _read_env(prefix: str, *suffixes: str) -> Optional[str]:
    for s in suffixes:
        v = os.environ.get(f"{prefix}_{s.upper()}", "")
        if v:
            return v
    return None


# --------------------------------------------------------------------------- #
# Zoho CRM                                                                    #
# --------------------------------------------------------------------------- #

_ZOHO_CACHE: Dict[str, Dict[str, Any]] = {}


def _ensure_zoho_token() -> Optional[str]:
    now = int(time.time())
    tinfo = _ZOHO_CACHE.get("z")
    if tinfo and tinfo.get("expires_in", 0) > now + 60:
        return tinfo["access_token"]

    for suffix in ("CLIENT_CREDENTIALS","OIDC_TOKEN"):
        cid, csec, rto = _read_env("ZOHO","CLIENT_ID","CLIENT_SECRET","REFRESH_TOKEN")
        if not (cid and csec and rto):
            continue
        break
    if not cid:
        return None
    import httpx as _hx
    try:
        async def _fetch():
            async with _hx.AsyncClient(timeout=20) as cli:
                r = await cli.post("https://accounts.zoho.com/oauth/v2/token", data={
                    "grant_type": "refresh_token","client_id":cid,"client_secret":csec,
                    "refresh_token":rto,
                })
            if r.status_code != 200:
                return None
            d = r.json()
            _ZOHO_CACHE["z"] = {"access_token": d["access_token"],
                                "expires_in": now + d.get("expires_in", 3600)}
            return d["access_token"]
        import asyncio as _aio
        loop = _aio.new_event_loop()
        try:
            return loop.run_until_complete(_fetch())
        finally:
            loop.close()
    except Exception:
        return None


def zoho_tool_schemas() -> List[Dict[str, Any]]:
    return [
        _tool_schema(
            "zoho_search_contacts",
            "Search or list Zoho CRM contacts.",
            {
                "type": "object",
                "properties": {
                    "email":    {"type":"string","description":"Exact email"},
                    "last_name":{"type":"string","description":"Keyword in last name"},
                    "phone":    {"type":"string","description":"Phone number"},
                    "name":     {"type":"string","description":"First or full name search"},
                    "limit":    {"type":"integer","default":20},
                },
            },
        ),
        _tool_schema(
            "zoho_crud_record",
            "Create or update a Zoho CRM record (Contact, Lead, Deal, Account).",
            {
                "type":"object",
                "properties":{
                    "module":{"type":"string","enum":["Contacts","Leads","Deals","Accounts"]},
                    "action":{"type":"string","enum":["create","update","delete"]},"id":{"type":"string"},
                    "data":{"type":"object","description":"Field key–value pairs."},
                },
            },
        ),
    ]


async def zoho_tool_call(name: str, args: Optional[Dict[str, Any]]) -> str:
    args = args or {}
    token = _ensure_zoho_token()
    if not token:
        return "ZohoError: credentials not configured."

    if name == "zoho_search_contacts":
        base = "https://www.zohoapis.com/crm/v3/Contacts"
        params: Dict[str, str] = {"page":"1","per_page":str(min(args.get("limit",20),50))}
        for k in ("email","last_name","phone","name"):
            if args.get(k):
                params[k] = str(args[k])
        import httpx as _hx
        async with _hx.AsyncClient(timeout=30) as cli:
            r = await cli.get(base, headers={"Authorization":f"Zoho-oauthtoken {token}"}, params=params)
        if r.status_code != 200:
            return f"Zoho HTTP {r.status_code}: {r.text[:512]}"
        records = [e for e in (r.json().get("data") or [])]
        lines = [f"{c.get('First_Name','')} {c.get('Last_Name','')} | {c.get('Email','-')} | {c.get('Phone','-')} | {c.get('Company','-')}"
                 for c in records]
        return "\n".join(lines) or "No contacts found."

    if name == "zoho_crud_record":
        module = args.get("module","")
        action = args.get("action","create")
        base = f"https://www.zohoapis.com/crm/v3/{module}"
        hdrs  = {"Authorization":f"Zoho-oauthtoken {token}","Content-Type":"application/json"}
        import httpx as _hx
        async with _hx.AsyncClient(timeout=30) as cli:
            if action == "create":
                r = await cli.post(base,   json=[args.get("data",{})], headers=hdrs)
            elif action == "update":
                r = await cli.put(f"{base}/{ args.get('id','')}",          json=args.get("data",{}), headers=hdrs)
            elif action == "delete":
                r = await cli.delete(f"{base}/{args.get('id','')}",        headers=hdrs)
            else:
                return f"zoho_crud_record unsupported action:{action}"
        if r.status_code not in (200, 201, 204):
            body = ""
            try: body=r.text[:512]
            except Exception: pass     # type:ignore[possibly-undefined]
            return f"Zoho err {r.status_code}: {body}"
        if r.status_code == 204:
            return f"Deleted {module} record."
        return json.dumps(r.json(), indent=2)

    return f"zoho_tool_call: unknown name '{name}'"


# --------------------------------------------------------------------------- #
# JIRA                                                                        #
# --------------------------------------------------------------------------- #

def jira_tool_schemas() -> List[Dict[str, Any]]:
    return [
        _tool_schema(
            "jira_create_ticket",
            "Create a new issue in JIRA (Cloud REST API v3).",
            {
                "type":"object",
                "properties":{
                    "project_key":{"type":"string"},"summary":{"type":"string"},
                    "description":{"type":"string"},"issuetype":{"type":"string","default":"Task"},
                    "assignee":{"type":"string"},"priority":{"type":"string","enum":["Lowest","Low","Medium","High","Highest"]},
                },
            },
        ),
        _tool_schema(
            "jira_query",
            "Search JIRA issues using JQL.",
            {
                "type":"object",
                "properties":{
                    "jql":{"type":"string"},"max_results":{"type":"integer","default":20},
                },
            },
        ),
    ]


def _jira_auth() -> Dict[str,str]:
    u = os.environ.get("JIRA_EMAIL","") or os.environ.get("JIRA_API_TOKEN_USER","")
    t = os.environ.get("JIRA_API_TOKEN","") or os.environ.get("JIRA_API_SECRET","")
    if not u or not t:
        raise ValueError("jira credentials missing (JIRA_API_TOKEN + JIRA_EMAIL)")
    h   = f"Basic {base64.b64encode(f'{u}:{t}'.encode()).decode()}"      # type: ignore[assignment]
    return {"Authorization":h,"Content-Type":"application/json"}


async def jira_tool_call(name: str, args: Optional[Dict[str, Any]]) -> str:
    args  = args or {}
    base_jira = os.environ.get("JIRA_BASE_URL","https://api.atlassian.com/ex/jira").rstrip("/")

    if name == "jira_create_ticket":
        payload = {
            "fields":{
                "project":{"key":args["project_key"]},"summary":args.get("summary",""),
                "issuetype":{"name":args.get("issuetype","Task")},
                "description":args.get("description","Created by ba-account-swarm"),
            },
        }
        if args.get("assignee"): payload["fields"]["assignee"]={"name":args["assignee"]}
        if args.get("priority"): payload["fields"]["priority"]={"name":args["priority"]}

        import httpx
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base_jira}/rest/api/3/issue",   json=payload,headers=_jira_auth())
        if r.status_code != 201:
            txt=""
            try:txt=r.text[:512]               # type: ignore[possibly-undefined]
            except Exception: pass              # type: ignore[possibly-undefined]
            return f"jira_create_ticket HTTP {r.status_code}: {txt}"
        d = r.json()
        return f"Created [{d.get('key','?')}]({d.get('htmlUrl','')})"

    if name == "jira_query":
        jql=args["jql"]
        mr=min(args.get("max_results",20),50)
        import httpx
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base_jira}/rest/api/3/search", json={"jql":jql,"maxResults":mr}, headers=_jira_auth())
        if r.status_code != 200:
            txt=""
            try:txt=r.text[:512]               # type: ignore[possibly-undefined]
            except Exception: pass              # type: ignore[possibly-undefined]
            return f"jira_query HTTP {r.status_code}: {txt}"
        issues = r.json().get("issues",[])
        lines=[f"[{i.get('key','?')}]{i['fields'].get('summary','')}" for i in issues]   # type: ignore[union-attr]
        return "\n".join(lines) or "No JIRA issues found."

    return f"jira_tool_call: unknown '{name}'"


# --------------------------------------------------------------------------- #
# Gmail                                                                       #
# --------------------------------------------------------------------------- #

def gmail_tool_schemas() -> List[Dict[str, Any]]:
    return [
        _tool_schema(
            "gmail_send_message",
            "Send an email via Google Gmail API.",
            {
                "type":"object",
                "properties":{
                    "to":{"type":"string"},"subject":{"type":"string"},
                    "body":{"type":"string"},"html":{"type":"boolean","default":False},
                    "cc":{"type":"string"},"bcc":{"type":"string"},
                },
            },
        ),
    ]


async def gmail_tool_call(name:str, args:Optional[Dict[str, Any]]) -> str:
    if name!="gmail_send_message":
        return f"gmail_tool_call unknown '{name}'"
    args=args or {}
    to_=args.get("to","")
    subj=args.get("subject","(no subject)")
    body=args.get("body","") or ""

    client_email=os.environ.get("GMAIL_CLIENT_EMAIL","")
    if not to_:
        return "gmail_send_message: 'to' required."
    mime=(
        f"From: {client_email or 'ba-swarm@localhost'}\n"
        f"To: {to_}\nSubject: {subj}\n"
        f"MIME-Version: 1.0\nContent-Type: {'text/html' if args.get('html') else 'text/plain; charset=utf-8'}\n\n{body}"
    )
    raw=base64.urlsafe_b64encode(mime.encode()).decode()

    client_email=os.environ.get("GMAIL_CLIENT_EMAIL","")
    key_path=os.environ.get("GMAIL_PRIVATE_KEY_PATH","")
    if not client_email or not key_path:
        return f"GMail send simulated for {to_}:\\n---\\n{mime}\\n---\\n(Configure GMAIL_CLIENT_EMAIL + GMAIL_PRIVATE_KEY_PATH to enable.)"

    import httpx
    url=f"https://gmail.googleapis.com/gmail/v1/users/{client_email}/messages/send"
    async with httpx.AsyncClient(timeout=30) as c:
        r=await c.post(url, json={"raw":raw})
    if r.status_code==200:
        return f"Gmail sent to {to_} (ID:{r.json().get('id','?')})."
    txt=""
    try:txt=r.text[:512]                   # type: ignore[possibly-undefined]
    except Exception: pass                  # type: ignore[possibly-undefined]
    return f"Gmail err {r.status_code}: {txt}"


# --------------------------------------------------------------------------- #
# Microsoft Graph                                                             #
# --------------------------------------------------------------------------- #

def graph_tool_schemas() -> List[Dict[str, Any]]:
    return [
        _tool_schema(
            "graph_list_calendar_events",
            "List Outlook calendar events via Microsoft Graph.",
            {
                "type":"object",
                "properties":{
                    "days_ahead":{"type":"integer","default":7},
                    "user_principal_name":{"type":"string"},
                 },
            },
        ),
        _tool_schema(
            "graph_get_profile",
            "Get Azure AD / Graph user profile information.",
            {
                "type":"object",
                "properties":{
                    "user_principal_name":{"type":"string","description":"UPN or email (omit for me)."},
                    "fields":{"type":"string","description":"Comma‑separated fields."},
                },
            },
        ),
    ]


async def _graph_token() -> Optional[str]:
    cid=os.environ.get("MICROSOFT_GRAPH_CLIENT_ID","")
    csec=os.environ.get("MICROSOFT_GRAPH_CLIENT_SECRET","")
    import httpx
    async with httpx.AsyncClient(timeout=20) as c:
        r =await c.post("https://login.microsoftonline.com/common/oauth2/v2.0/token",
                        data={"grant_type":"client_credentials","client_id":cid,"client_secret":csec,
                              "scope":"https://graph.microsoft.com/.default"})
    if r.status_code!=200: return None
    return r.json().get("access_token")

async def graph_tool_call(name:str, args:Optional[Dict[str, Any]]) -> str:
    tok=await _graph_token()
    if not tok:
        return "GraphError: credentials missing (MICROSOFT_GRAPH_CLIENT_ID / _SECRET)."

    if name=="graph_list_calendar_events":
        from datetime import datetime,timedelta
        days=args.get("days_ahead",7)
        st=(datetime.utcnow()).strftime("%Y-%m-%dT%H:%MZ")
        et=(datetime.utcnow()+timedelta(days=days)).strftime("%Y-%m-%dT%H:%MZ")
        url=f"https://graph.microsoft.com/v1.0/me/calendarview?startdatetime={st}&enddatetime={et}"
        import httpx
        async with httpx.AsyncClient(timeout=30) as c:
            r=await c.get(url,headers={"Authorization":f"Bearer {tok}"})
        if r.status_code!=200:
            return f"graph_calendar err {r.status_code}"
        evts=[e for e in (r.json().get("value") or [])]
        lines=[f"[{e.get('start',{}).get('dateTime','?')}] {e.get('subject','')}" for e in evts]  # type:ignore[union-attr]
        return "\n".join(lines) or "No events."

    if name=="graph_get_profile":
        upn=args.get("user_principal_name","") or "me"
        flds=args.get("fields","")
        s=f"?$select={flds or 'displayName,mail,userPrincipalName'}" if flds else "?$select=displayName,mail,userPrincipalName"
        import httpx as _hx
        url=f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote_plus(upn)}{s}"      # type:ignore[name-defined]
        async with _hx.AsyncClient(timeout=30) as c:
            r=await c.get(url,headers={"Authorization":f"Bearer {tok}"})
        if r.status_code!=200:return f"graph_profile err {r.status_code}"
        u=r.json()   # type: ignore[assignment]
        parts=[]
        for f in (flds.split(",") if flds else("displayName","mail","userPrincipalName")):
            f=f.strip()
            if f and u.get(f):parts.append(f"{f}: {u[f]}")    # type:ignore[index]
        return "; ".join(parts) or json.dumps(u,indent=2,default=str)

    return f"graph_tool_call unknown '{name}'"


# --------------------------------------------------------------------------- #
# WhatsApp                                                                    #
# --------------------------------------------------------------------------- #

def whatsapp_tool_schemas() -> List[Dict[str, Any]]:
    return [
        _tool_schema(
            "send_whatsapp",
            "Send a WhatsApp message via Meta Cloud API.",
            {
                "type":"object",
                "properties":{
                    "to_phone":{"type":"string"},"message":{"type":"string"},
                    "whatsapp_id":{"type":"string","description":"phone number ID (fallback env)."},
                     "access_token":{"type":"string","description":"bearer token (fallback env)."},
                },
            },
        ),
    ]


async def whatsapp_tool_call(name:str, args:Optional[Dict[str, Any]]) -> str:
    if name!="send_whatsapp": return f"whatsapp_tool_call unknown '{name}'"
    args=args or {}
    to=args.get("to_phone","")
    msg=args.get("message","")
    if not to or not msg:
        return "send_whatsapp: 'to_phone' + 'message' required."
    wid=(args.get("whatsapp_id") or os.environ.get("WHATSAPP_BUSINESS_PHONE_NUMBER_ID",""))
    tok=(args.get("access_token", "") or os.environ.get("WHATSAPP_WHATSAPP_ACCESS_TOKEN",""))
    if not wid or not tok:
        return "WhatsAppError: phone ID + access token not configured."

    import httpx as _hx
    url=f"https://graph.facebook.com/v18.0/{wid}/messages"
    payload={"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":msg}}
    async with _hx.AsyncClient(timeout=30) as c:
        r=await c.post(url,json=payload,headers={"Authorization":f"Bearer {tok}"})
    if r.status_code==200:
           mid=(r.json().get("messages") or [{}])[0].get("id","?")
           return f"WhatsApp sent to {to} (ID:{mid})."
    return f"WhatsApp err {r.status_code}: {r.text[:512] if hasattr(r,'text') else ''}"


# --------------------------------------------------------------------------- #
# SQLite query helper                                                         #
# --------------------------------------------------------------------------- #


def sqlite_tool_schemas() -> List[Dict[str, Any]]:
    return [
        _tool_schema(
            "sqlite_query",
            "Execute a read‑only SQL SELECT against the local ba‑account-swarm database.",
            {
                "type":"object",
                "properties":{
                    "query":{"type":"string","description":"SQL SELECT statement."},
                    "params":{"type":"array","items":{"type":"string"},
                               "description":"positional parameters."},
                 },
            },
        ),
    ]


def _db_path() -> str:
    from ..app.db import get_db_path         # type: ignore[import-not-found,no-redef]  — safe guard
    return str(get_db_path())               # type: ignore[no-untyped-call]
