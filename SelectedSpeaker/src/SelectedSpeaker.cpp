// SelectedSpeaker — RE_Kenshi plugin for SentientSands.
//
// Copyright (C) 2026 SentientSands contributors
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.
//
// ---------------------------------------------------------------------------
// WHY THIS EXISTS
//
// The stock SentientSands.dll always reports playerCharacters[0] — squad 1,
// slot 1 — as the character talking to NPCs. This plugin reports whichever
// player character is actually selected in-game to the SentientSands Python
// server, which layers it over the stock record.
//
// It never touches SentientSands.dll. If this plugin is absent, disabled, or
// crashes, the server's freshness window lapses and the mod behaves exactly as
// it always did.
//
// The character serialiser below mirrors GetDetailedContext() in the public
// SentientSands source (src/Context.cpp, GPL-3.0) so the JSON the server
// receives here is shaped exactly like the JSON it already receives on
// POST /context.
//
// TWO THREADING RULES, both learned from that same source:
//   1. Inventory may only be walked on Kenshi's main thread. We serialise
//      inside the PlayerInterface::update hook, which runs there.
//   2. The HTTP POST must NOT run on that thread — WinHttp blocks for up to
//      5 seconds, which would stall the game whenever the server is down.
//      We hand the finished JSON to a worker thread instead.
// ---------------------------------------------------------------------------

#include <Debug.h>

#include <kenshi/CharStats.h>
#include <kenshi/Character.h>
#include <kenshi/Faction.h>
#include <kenshi/GameData.h>
#include <kenshi/GameWorld.h>
#include <kenshi/InstanceID.h>
#include <kenshi/Inventory.h>
#include <kenshi/Item.h>
#include <kenshi/MedicalSystem.h>
#include <kenshi/PlayerInterface.h>
#include <kenshi/RaceData.h>
#include <kenshi/RootObject.h>
#include <kenshi/util/hand.h>
#include <kenshi/util/lektor.h>

#include <core/Functions.h>

#include <cstdio>
#include <sstream>
#include <string>
#include <vector>

#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <winhttp.h>

// ─── CONFIG ─────────────────────────────────────────────────────────────────

// Server-side freshness window is 10s (SELECTED_TTL_SECONDS in ss_identity.py).
// Posting every 2s keeps the report comfortably fresh with room for a few
// dropped requests before the server falls back to the stock speaker.
static const DWORD POST_INTERVAL_MS = 2000;

static const wchar_t *SERVER_HOST = L"127.0.0.1";
static const INTERNET_PORT SERVER_PORT = 5000;
static const wchar_t *ENDPOINT = L"/selected_character";

// ─── SHARED STATE (main thread produces, worker thread consumes) ────────────

static CRITICAL_SECTION g_pendingLock;
static std::string g_pendingJson;
static bool g_hasPending = false;
static HANDLE g_wakeWorker = NULL;

// Main-thread only.
static hand g_latchedHand;
static DWORD g_lastPostTick = 0;
static bool g_lastPostWasEmpty = false;

// ─── SMALL HELPERS ──────────────────────────────────────────────────────────

static std::string ToStr(int v) {
  std::ostringstream ss;
  ss << v;
  return ss.str();
}

static std::string ToStr(float v) {
  // JSON has no inf/nan literals: emitting one would make the entire payload
  // unparseable and the server would silently drop every field, not just this
  // one. A damaged character is not worth losing the whole report over.
  if (!(v == v) || v > 3.0e38f || v < -3.0e38f)
    return "0";
  std::ostringstream ss;
  ss << v;
  return ss.str();
}

static std::string EscapeJSON(const std::string &s) {
  std::string out;
  out.reserve(s.size() + 8);
  for (size_t i = 0; i < s.size(); ++i) {
    unsigned char c = (unsigned char)s[i];
    switch (c) {
    case '\"': out += "\\\""; break;
    case '\\': out += "\\\\"; break;
    case '\b': out += "\\b";  break;
    case '\f': out += "\\f";  break;
    case '\n': out += "\\n";  break;
    case '\r': out += "\\r";  break;
    case '\t': out += "\\t";  break;
    default:
      if (c < 0x20) {
        // Control characters must be escaped or Python's json.loads rejects
        // the whole payload and we would silently lose every update.
        char buf[8];
        sprintf_s(buf, sizeof(buf), "\\u%04x", (int)c);
        out += buf;
      } else {
        out += (char)c;
      }
    }
  }
  return out;
}

static std::string SlotToString(AttachSlot slot) {
  switch (slot) {
  case ATTACH_WEAPON:    return "weapon";
  case ATTACH_BACK:      return "back";
  case ATTACH_HAIR:      return "hair";
  case ATTACH_HAT:       return "hat";
  case ATTACH_EYES:      return "eyes";
  case ATTACH_BODY:      return "body";
  case ATTACH_LEGS:      return "legs";
  case ATTACH_SHIRT:     return "shirt";
  case ATTACH_BOOTS:     return "boots";
  case ATTACH_GLOVES:    return "gloves";
  case ATTACH_NECK:      return "neck";
  case ATTACH_BACKPACK:  return "backpack";
  case ATTACH_BEARD:     return "beard";
  case ATTACH_BELT:      return "belt";
  case ATTACH_LEFT_ARM:  return "left_arm";
  case ATTACH_RIGHT_ARM: return "right_arm";
  case ATTACH_LEFT_LEG:  return "left_leg";
  case ATTACH_RIGHT_LEG: return "right_leg";
  default:               return "none";
  }
}

// Kenshi pointers are occasionally torn down mid-frame; the stock source guards
// every dereference with this same low-address sanity check.
static bool SanePtr(const void *p) { return p && (uintptr_t)p >= 0x1000; }

// ─── SERIALISER ─────────────────────────────────────────────────────────────
// Emits ONLY per-character keys. World state (day, hour, gamespeed, is_paused,
// environment, events, memories, nearby, squad) is deliberately omitted: the
// stock DLL already streams it to POST /context, the server merges ours on top
// of that, and duplicating it here would let the two senders drift apart.

static void AppendItems(Character *npc, std::string &json) {
  json += "\"inventory\": [";
  Inventory *inv = npc->getInventory();
  std::vector<Item *> items;

  if (SanePtr(inv)) {
    lektor<InventorySection *> &sections = inv->sectionsInSearchOrder;
    for (uint32_t s = 0; s < sections.size(); ++s) {
      InventorySection *sect = sections[s];
      if (!SanePtr(sect))
        continue;
      const Ogre::vector<InventorySection::SectionItem>::type &secItems = sect->getItems();
      for (uint32_t i = 0; i < secItems.size(); ++i)
        if (secItems[i].item)
          items.push_back(secItems[i].item);
    }
  }

  ContainerItem *backpack = npc->hasABackpackOn();
  if (SanePtr(backpack) && SanePtr(backpack->inventory)) {
    lektor<InventorySection *> &bpSections = backpack->inventory->sectionsInSearchOrder;
    for (uint32_t s = 0; s < bpSections.size(); ++s) {
      InventorySection *sect = bpSections[s];
      if (!SanePtr(sect))
        continue;
      const Ogre::vector<InventorySection::SectionItem>::type &secItems = sect->getItems();
      for (uint32_t i = 0; i < secItems.size(); ++i)
        if (secItems[i].item)
          items.push_back(secItems[i].item);
    }
  }

  for (size_t i = 0; i < items.size(); ++i) {
    if (i > 0)
      json += ",";
    int price = 0;
    try {
      price = items[i]->getValueSingle(false);
    } catch (...) {
    }
    json += "{\"name\": \"" + EscapeJSON(items[i]->getName()) +
            "\", \"count\": " + ToStr((int)items[i]->quantity) +
            ", \"price\": " + ToStr(price) +
            ", \"equipped\": " + (items[i]->isEquipped ? "true" : "false") +
            ", \"slot\": \"" + SlotToString(items[i]->slotType) + "\"}";
  }
  json += "]";
}

static std::string SerialiseCharacter(Character *npc) {
  if (!SanePtr(npc))
    return "";

  std::string json = "{";

  // --- State (first, so the server can gate on dead/KO before reading on) ---
  std::string charState = "normal";
  bool isDead = false, isUnconcious = false;
  try {
    isDead = npc->isDead();
    if (isDead) {
      charState = "dead";
    } else {
      isUnconcious = npc->isUnconcious();
      if (isUnconcious) {
        charState = "unconscious";
      } else if (npc->inSomething == IN_PRISON) {
        charState = "imprisoned";
      } else {
        try {
          SlaveStateEnum slaveState = npc->isSlave();
          if (slaveState != 0)
            charState = npc->isChainedMode() ? "enslaved" : "escaped-slave";
        } catch (...) {
        }
      }
    }
  } catch (...) {
  }
  json += "\"character_state\": \"" + charState + "\",";
  json += "\"is_incapacitated\": " +
          std::string((isDead || isUnconcious) ? "true" : "false") + ",";

  // --- Identity ---
  std::string name = "Unknown";
  try {
    name = npc->getName();
    if (name.empty() || name == "Unknown Entity" || name == "Unknown") {
      if (!npc->displayName.empty())
        name = npc->displayName;
      else if (SanePtr(npc->data) && !npc->data->name.empty())
        name = npc->data->name;
    }
  } catch (...) {
  }
  if (name.empty())
    return ""; // Nameless speaker is worse than no override at all.
  json += "\"name\": \"" + EscapeJSON(name) + "\",";

  InstanceID *iid = npc->getInstanceID();
  if (SanePtr(iid) && !iid->uid.empty())
    json += "\"id\": \"" + EscapeJSON(iid->uid) + "\",";
  else
    json += "\"id\": \"hand_" + ToStr((int)npc->getHandle().serial) + "\",";

  // Kayak resolves the player's own lore entity by id, then by name; storage_id
  // mirrors the stock DLL, which uses the plain name as the folder key.
  json += "\"storage_id\": \"" + EscapeJSON(name) + "\",";

  RaceData *race = NULL;
  try {
    race = npc->getRace() ? npc->getRace() : npc->myRace;
  } catch (...) {
  }
  std::string raceName = "Unknown";
  if (SanePtr(race) && SanePtr(race->data)) {
    if (!race->data->name.empty())
      raceName = race->data->name;
    else if (!race->data->stringID.empty())
      raceName = race->data->stringID;
  }
  json += "\"race\": \"" + EscapeJSON(raceName) + "\",";

  std::string gender = "male";
  try {
    gender = npc->isFemale() ? "female" : "male";
  } catch (...) {
  }
  if (npc->sex == "female" || npc->sex == "male")
    gender = npc->sex;
  json += "\"gender\": \"" + gender + "\",";

  Faction *faction = NULL;
  try {
    faction = npc->getFaction();
  } catch (...) {
  }
  std::string factionName = "Neutral", factionID = "Neutral";
  if (SanePtr(faction)) {
    std::string fn = faction->getName();
    if (!fn.empty() && fn != "Unknown")
      factionName = fn;
    else if (SanePtr(faction->data) && !faction->data->name.empty())
      factionName = faction->data->name;

    if (SanePtr(faction->data) && !faction->data->stringID.empty())
      factionID = faction->data->stringID;
    else
      factionID = factionName;
  }
  json += "\"faction\": \"" + EscapeJSON(factionName) + "\",";
  json += "\"factionID\": \"" + EscapeJSON(factionID) + "\",";
  json += "\"origin_faction\": \"" + EscapeJSON(factionName) + "\",";

  std::string job = "None";
  try {
    int jobCount = npc->getPermajobCount();
    if (jobCount > 0) {
      job = "";
      for (int i = 0; i < jobCount; ++i) {
        std::string jName = npc->getPermajobName(i);
        if (!jName.empty()) {
          if (!job.empty())
            job += ", ";
          job += jName;
        }
      }
      if (job.empty())
        job = "None";
    }
  } catch (...) {
  }
  json += "\"job\": \"" + EscapeJSON(job) + "\",";

  int money = 0;
  try {
    money = npc->getMoney();
    if (money <= 0 && SanePtr(npc->getOwnerships()))
      money = npc->getOwnerships()->getMoney();
  } catch (...) {
  }
  json += "\"money\": " + ToStr(money) + ",";

  // --- Stats ---
  CharStats *stats = npc->getStats();
  if (SanePtr(stats)) {
    json += "\"stats\": {";
    json += "\"strength\": "      + ToStr((int)stats->_strength) + ",";
    json += "\"dexterity\": "     + ToStr((int)stats->_dexterity) + ",";
    json += "\"toughness\": "     + ToStr((int)stats->_toughness) + ",";
    json += "\"perception\": "    + ToStr((int)stats->perception) + ",";
    json += "\"melee_attack\": "  + ToStr((int)stats->getStat(STAT_MELEE_ATTACK, false)) + ",";
    json += "\"melee_defence\": " + ToStr((int)stats->getStat(STAT_MELEE_DEFENCE, false)) + ",";
    json += "\"athletics\": "     + ToStr((int)stats->getStat(STAT_ATHLETICS, false));
    json += "},";
  }

  // --- Medical ---
  MedicalSystem *med = npc->getMedical();
  if (SanePtr(med)) {
    json += "\"medical\": {";
    json += "\"blood\": "      + ToStr((int)med->blood) + ",";
    json += "\"max_blood\": "  + ToStr((int)med->getMaxBlood()) + ",";
    json += "\"blood_rate\": " + ToStr(med->currentBleedRate) + ",";
    // Kenshi stores hunger as a deficit (0 full … 300 starving); 'fed' covers
    // food still being digested. The server expects the stock DLL's convention.
    float hungerVal = (300.0f - med->hunger) + med->fed;
    if (hungerVal < 0)
      hungerVal = 0;
    json += "\"hunger\": " + ToStr((int)hungerVal) + ",";
    json += "\"is_unconscious\": " +
            std::string(med->unconcious ? "true" : "false") + ",";
    json += "\"limbs\": {";
    // getPart is overloaded on RobotLimbs::Limb and on a raw index; the cast
    // picks the index form explicitly rather than leaning on overload
    // resolution to reject the enum for us.
    MedicalSystem::HealthPartStatus *parts[6] = {
        med->getPart((unsigned __int64)0), med->getPart((unsigned __int64)1),
        med->leftArm,                      med->rightArm,
        med->leftLeg,                      med->rightLeg};
    static const char *partNames[6] = {"head",      "stomach", "left_arm",
                                       "right_arm", "left_leg", "right_leg"};
    for (int i = 0; i < 6; ++i) {
      if (i > 0)
        json += ",";
      MedicalSystem::HealthPartStatus *p = parts[i];
      json += "\"" + std::string(partNames[i]) + "\": " +
              ToStr(p ? (int)p->flesh : 100) + ",";
      json += "\"" + std::string(partNames[i]) + "_max\": " +
              ToStr(p ? (int)p->maxHealth() : 100);
    }
    json += "}},";
  }

  AppendItems(npc, json);
  json += "}";
  return json;
}

// ─── SELECTION LATCH ────────────────────────────────────────────────────────

static bool IsPlayerCharacter(Character *c) {
  if (!SanePtr(c) || !SanePtr(ou) || !SanePtr(ou->player))
    return false;
  lektor<Character *> &roster = ou->player->playerCharacters;
  for (uint32_t i = 0; i < roster.size(); ++i)
    if (roster[i] == c)
      return true;
  return false;
}

// Returns the character that should be speaking, or NULL to fall back.
//
// The latch is the whole point. Kenshi's selection follows the mouse, and
// SentientSands' own chat window is opened by clicking an NPC — the stock DLL
// logs "Falling back to selection or speaker" for exactly that reason. So the
// selection at the moment of a conversation may well be the NPC. We therefore
// remember the last selection that was one of OUR characters and ignore
// everything else, including an empty selection.
static Character *ResolveSpeaker() {
  if (!SanePtr(ou) || !SanePtr(ou->player))
    return NULL;

  Character *sel = NULL;
  try {
    sel = ou->player->selectedCharacter.getCharacter();
  } catch (...) {
    sel = NULL;
  }

  if (IsPlayerCharacter(sel))
    g_latchedHand = sel->getHandle();

  if (g_latchedHand.isNull() || !g_latchedHand.isValid())
    return NULL;

  Character *latched = NULL;
  try {
    latched = g_latchedHand.getCharacter();
  } catch (...) {
    latched = NULL;
  }

  // Recruited away, killed and cleaned up, or the save was reloaded.
  if (!IsPlayerCharacter(latched)) {
    g_latchedHand.setNull();
    return NULL;
  }
  return latched;
}

// ─── HTTP WORKER ────────────────────────────────────────────────────────────

static void PostJson(const std::string &jsonData) {
  HINTERNET hSession = NULL, hConnect = NULL, hRequest = NULL;
  BOOL ok = FALSE;

  hSession = WinHttpOpen(L"SelectedSpeaker/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                         WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
  if (hSession) {
    WinHttpSetTimeouts(hSession, 3000, 3000, 3000, 3000);
    hConnect = WinHttpConnect(hSession, SERVER_HOST, SERVER_PORT, 0);
  }
  if (hConnect)
    hRequest = WinHttpOpenRequest(hConnect, L"POST", ENDPOINT, NULL,
                                  WINHTTP_NO_REFERER,
                                  WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
  if (hRequest)
    ok = WinHttpSendRequest(hRequest, L"Content-Type: application/json\r\n",
                            (DWORD)-1L, (LPVOID)jsonData.c_str(),
                            (DWORD)jsonData.length(), (DWORD)jsonData.length(), 0);
  if (ok)
    WinHttpReceiveResponse(hRequest, NULL);

  // A failed POST is not worth logging every 2 seconds — the server simply
  // lets its freshness window lapse and reverts to the stock speaker.
  if (hRequest) WinHttpCloseHandle(hRequest);
  if (hConnect) WinHttpCloseHandle(hConnect);
  if (hSession) WinHttpCloseHandle(hSession);
}

// One long-lived worker rather than a thread per POST: at one message every
// two seconds for a whole play session, thread churn would be the larger cost.
// It runs for the lifetime of the process — RE_Kenshi has no plugin-teardown
// callback to shut it down from, and Kenshi exiting takes it with the process.
static DWORD WINAPI WorkerThread(LPVOID) {
  for (;;) {
    WaitForSingleObject(g_wakeWorker, INFINITE);

    std::string payload;
    EnterCriticalSection(&g_pendingLock);
    if (g_hasPending) {
      payload.swap(g_pendingJson);
      g_pendingJson.clear();
      g_hasPending = false;
    }
    LeaveCriticalSection(&g_pendingLock);

    if (!payload.empty())
      PostJson(payload);
  }
  return 0;
}

static void QueuePost(const std::string &json) {
  EnterCriticalSection(&g_pendingLock);
  // Newest wins: a queued-but-unsent report is already stale by definition.
  g_pendingJson = json;
  g_hasPending = true;
  LeaveCriticalSection(&g_pendingLock);
  SetEvent(g_wakeWorker);
}

// ─── UPDATE HOOK ────────────────────────────────────────────────────────────

void (*PlayerInterface_update_orig)(PlayerInterface *) = NULL;

void PlayerInterface_update_hook(PlayerInterface *thisptr) {
  PlayerInterface_update_orig(thisptr);

  // Everything below runs on Kenshi's main thread — the only place the
  // inventory walk above is safe.
  DWORD now = GetTickCount();
  if (now - g_lastPostTick < POST_INTERVAL_MS)
    return;
  g_lastPostTick = now;

  try {
    Character *speaker = ResolveSpeaker();
    if (!speaker) {
      // Tell the server once, so it drops back to the stock speaker
      // immediately instead of waiting out its freshness window.
      if (!g_lastPostWasEmpty) {
        QueuePost("{\"clear\": true}");
        g_lastPostWasEmpty = true;
      }
      return;
    }
    std::string json = SerialiseCharacter(speaker);
    if (json.empty())
      return;
    QueuePost(json);
    g_lastPostWasEmpty = false;
  } catch (...) {
    // Never let a serialisation problem take the game's update loop with it.
  }
}

// ─── ENTRY POINT ────────────────────────────────────────────────────────────

__declspec(dllexport) void startPlugin() {
  InitializeCriticalSection(&g_pendingLock);
  g_wakeWorker = CreateEvent(NULL, FALSE, FALSE, NULL);
  if (!g_wakeWorker) {
    ErrorLog("SelectedSpeaker: could not create worker event!");
    return;
  }
  CreateThread(NULL, 0, WorkerThread, NULL, 0, NULL);

  if (KenshiLib::SUCCESS !=
      KenshiLib::AddHook(KenshiLib::GetRealAddress(&PlayerInterface::update),
                         PlayerInterface_update_hook,
                         &PlayerInterface_update_orig))
    ErrorLog("SelectedSpeaker: could not hook PlayerInterface::update!");
}
