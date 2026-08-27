-- ============================================================
-- SERIAL NETWORK SWITCH — Retro Gadgets
-- ============================================================
-- Build requirements for this gadget:
--   4x Serial      (gdt.Serial0 .. gdt.Serial3)
--   4x Led         (gdt.Led0 .. gdt.Led3)          -- activity light per port
--   1+ Screen      (gdt.Screen0, Screen1, ...)
--   1x VideoChip   (gdt.VideoChip0)
--   1x ROM chip    (for gdt.ROM.System.SpriteSheets["StandardFont"])
--
-- IN-GAME WIRING (done by hand in the Multitool, not in code):
--   Wire Serial0 -> CPU event channel 1
--   Wire Serial1 -> CPU event channel 2
--   Wire Serial2 -> CPU event channel 3
--   Wire Serial3 -> CPU event channel 4
--
-- PROTOCOL (all lines terminated with \n):
--   HELLO:<id>              — device heartbeat/announce
--   ARP:WHO-HAS:<id>       — switch asks "who is <id>?"
--   ARP:IS-AT:<id>         — device replies "I am <id>"
--   DATA:<src>:<dst>:<msg> — payload frame
--   "ALL" as dst means broadcast
-- ============================================================

local PORT_COUNT = 4

-- ---- EDIT THESE to match your com0com/socat virtual port numbers ----
local COM_PORTS = { 3, 5, 7, 9 }
-- -----------------------------------------------------------------

-- How many ticks before a device is considered disconnected
local HEARTBEAT_TIMEOUT = 300  -- ~5 seconds at 60fps

-- How many ticks to wait for an ARP reply before dropping
local ARP_TIMEOUT = 60  -- ~1 second at 60fps

-- ============================================================
-- HELPERS
-- ============================================================

local function getComp(prefix, index)
	return gdt[prefix .. tostring(index)]
end

local function trim(s)
	if s == nil then return nil end
	s = string.gsub(s, "^%s+", "")
	s = string.gsub(s, "%s+$", "")
	s = string.gsub(s, "%c+", "")
	return s
end

-- Debug logging — prints to the in-game console (Multitool debug view)
local function dbg(msg)
	print("[SWITCH] " .. msg)
end

-- ============================================================
-- INIT
-- ============================================================

local videochip = gdt.VideoChip0
local font = gdt.ROM.System.SpriteSheets["StandardFont"]

-- Assign VideoChip to all screens
local screenIndex = 0
while gdt["Screen" .. screenIndex] ~= nil do
	gdt["Screen" .. screenIndex].VideoChip = videochip
	screenIndex = screenIndex + 1
end

dbg("Initializing " .. PORT_COUNT .. " ports...")

local ports = {}
for i = 1, PORT_COUNT do
	local serialMod = getComp("Serial", i - 1)
	serialMod.Port = COM_PORTS[i]
	serialMod.ReceiveMode = SerialReceiveMode.Lines

	ports[i] = {
		serial = serialMod,
		led = getComp("Led", i - 1),
		ledTimer = 0,
		-- Device presence tracking
		deviceId = nil,        -- the ID announced by HELLO
		lastSeen = -9999,      -- tick when last HELLO received
		alive = false,         -- true if within HEARTBEAT_TIMEOUT
	}
	dbg("  Port " .. i .. " -> COM" .. COM_PORTS[i])
end

-- MAC/id learning table: device id string -> port index
local addrTable = {}

-- Rolling log of recent switching decisions, newest first
local logLines = {}
local MAX_LOG_LINES = 20

-- ARP pending queue: list of {dst, srcPort, frame, sentTick}
local arpQueue = {}

-- Global tick counter
local tickCount = 0

-- ============================================================
-- LOGGING
-- ============================================================

local function pushLog(line)
	table.insert(logLines, 1, line)
	while #logLines > MAX_LOG_LINES do
		table.remove(logLines)
	end
end

local function flashPort(i)
	ports[i].led.State = true
	ports[i].ledTimer = 6
end

-- ============================================================
-- ARP LOGIC
-- ============================================================

local function sendArpRequest(dst, excludePort)
	dbg("ARP: WHO-HAS " .. dst .. " (excluding P" .. excludePort .. ")")
	for i = 1, PORT_COUNT do
		if i ~= excludePort and ports[i].alive then
			ports[i].serial:Println("ARP:WHO-HAS:" .. dst)
			flashPort(i)
		end
	end
end

local function handleArpReply(srcPort, id)
	dbg("ARP: IS-AT " .. id .. " on P" .. srcPort)
	addrTable[id] = srcPort
	pushLog("ARP: " .. id .. " is at P" .. srcPort)

	-- Check pending queue for frames waiting on this id
	local remaining = {}
	for _, entry in ipairs(arpQueue) do
		if entry.dst == id then
			dbg("ARP: Delivering buffered frame to " .. id .. " via P" .. srcPort)
			ports[srcPort].serial:Println(entry.frame)
			flashPort(srcPort)
			pushLog(entry.src .. " -> " .. id .. "  (fwd P" .. srcPort .. ", post-ARP)")
		else
			table.insert(remaining, entry)
		end
	end
	arpQueue = remaining
end

local function processArpTimeouts()
	local remaining = {}
	for _, entry in ipairs(arpQueue) do
		if (tickCount - entry.sentTick) > ARP_TIMEOUT then
			dbg("ARP: TIMEOUT for " .. entry.dst .. " — dropping frame from " .. entry.src)
			pushLog(entry.src .. " -> " .. entry.dst .. "  (ARP timeout, dropped)")
		else
			table.insert(remaining, entry)
		end
	end
	arpQueue = remaining
end

-- ============================================================
-- FRAME HANDLING
-- ============================================================

local function handleHello(srcPort, id)
	dbg("HELLO from '" .. id .. "' on P" .. srcPort)
	ports[srcPort].deviceId = id
	ports[srcPort].lastSeen = tickCount
	ports[srcPort].alive = true
	addrTable[id] = srcPort
end

local function handleDataFrame(srcPort, line)
	-- Parse DATA:<src>:<dst>:<msg>
	local src, dst, msg = string.match(line, "^DATA:(.-):(.-):(.*)")
	if src == nil or dst == nil then
		dbg("BAD DATA FRAME on P" .. srcPort .. ": " .. line)
		pushLog("(bad frame on P" .. srcPort .. ")")
		return
	end

	src = trim(src)
	dst = trim(dst)

	dbg("DATA from " .. src .. " to " .. dst .. " on P" .. srcPort .. ": " .. (msg or ""))

	-- Learn source
	addrTable[src] = srcPort
	ports[srcPort].deviceId = src
	ports[srcPort].lastSeen = tickCount
	ports[srcPort].alive = true
	flashPort(srcPort)

	-- Broadcast
	if dst == "ALL" then
		dbg("  -> Broadcasting to all other ports")
		for i = 1, PORT_COUNT do
			if i ~= srcPort then
				ports[i].serial:Println(line)
				flashPort(i)
			end
		end
		pushLog(src .. " -> ALL  (broadcast)")
		return
	end

	-- Known destination: forward directly
	local knownPort = addrTable[dst]
	if knownPort ~= nil and knownPort ~= srcPort then
		dbg("  -> Forwarding to P" .. knownPort .. " (known)")
		ports[knownPort].serial:Println(line)
		flashPort(knownPort)
		pushLog(src .. " -> " .. dst .. "  (fwd P" .. knownPort .. ")")
		return
	end

	-- Unknown destination: ARP instead of blind flood
	dbg("  -> Unknown dst '" .. dst .. "', sending ARP WHO-HAS")
	sendArpRequest(dst, srcPort)

	-- Buffer the frame while waiting for ARP reply
	table.insert(arpQueue, {
		dst = dst,
		src = src,
		frame = line,
		sentTick = tickCount,
	})
	pushLog(src .. " -> " .. dst .. "  (ARP sent, buffered)")
end

-- ============================================================
-- INCOMING LINE DISPATCHER
-- ============================================================

local function handleLine(srcPort, rawLine)
	local line = trim(rawLine)
	if line == nil or line == "" then return end

	dbg("RX P" .. srcPort .. ": " .. line)

	-- HELLO:<id>
	local helloId = string.match(line, "^HELLO:(.*)")
	if helloId then
		handleHello(srcPort, trim(helloId))
		return
	end

	-- ARP:IS-AT:<id>
	local arpId = string.match(line, "^ARP:IS%-AT:(.*)")
	if arpId then
		handleArpReply(srcPort, trim(arpId))
		return
	end

	-- DATA:<src>:<dst>:<msg>
	if string.sub(line, 1, 5) == "DATA:" then
		handleDataFrame(srcPort, line)
		return
	end

	-- Legacy format (SRC:DST:MSG without DATA: prefix) — treat as data
	local src, dst, msg = string.match(line, "^(.-):(.-):(.*)$")
	if src and dst then
		dbg("  (legacy format, converting to DATA:)")
		handleDataFrame(srcPort, "DATA:" .. line)
		return
	end

	dbg("  UNKNOWN frame type: " .. line)
	pushLog("(unknown: " .. line .. ")")
end

-- ============================================================
-- EVENT CHANNELS
-- ============================================================

function eventChannel1(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do
			handleLine(1, line)
		end
	end
end

function eventChannel2(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do
			handleLine(2, line)
		end
	end
end

function eventChannel3(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do
			handleLine(3, line)
		end
	end
end

function eventChannel4(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do
			handleLine(4, line)
		end
	end
end

-- ============================================================
-- DISPLAY
-- ============================================================

local LINE_HEIGHT = 8
local MARGIN = 4

local function drawUI()
	videochip:Clear(color.black)

	local y = MARGIN
	videochip:DrawText(vec2(MARGIN, y), font, "-- SERIAL SWITCH --", color.white, color.black)
	y = y + LINE_HEIGHT * 2

	-- Port status with device presence
	for i = 1, PORT_COUNT do
		local p = ports[i]
		local linkUp = p.serial.IsActive
		local devAlive = p.alive
		local statusText
		local statusColor

		if devAlive then
			statusText = "ONLINE"
			statusColor = color.green
		elseif linkUp then
			statusText = "LINK"
			statusColor = color.yellow
		else
			statusText = "DOWN"
			statusColor = color.red
		end

		local devName = p.deviceId or "---"
		videochip:DrawText(vec2(MARGIN, y), font, "P" .. i, color.white, color.black)
		videochip:DrawText(vec2(MARGIN + 16, y), font, statusText, statusColor, color.black)
		videochip:DrawText(vec2(MARGIN + 56, y), font, devName, color.grey, color.black)
		y = y + LINE_HEIGHT
	end

	-- MAC address table
	y = y + LINE_HEIGHT
	videochip:DrawText(vec2(MARGIN, y), font, "-- MAC TABLE --", color.yellow, color.black)
	y = y + LINE_HEIGHT

	local hasEntries = false
	for id, port in pairs(addrTable) do
		hasEntries = true
		videochip:DrawText(vec2(MARGIN, y), font, id .. " -> P" .. port, color.yellow, color.black)
		y = y + LINE_HEIGHT
	end
	if not hasEntries then
		videochip:DrawText(vec2(MARGIN, y), font, "(empty)", color.grey, color.black)
		y = y + LINE_HEIGHT
	end

	-- ARP queue
	if #arpQueue > 0 then
		y = y + LINE_HEIGHT
		videochip:DrawText(vec2(MARGIN, y), font, "-- ARP PENDING --", color.magenta, color.black)
		y = y + LINE_HEIGHT
		for _, entry in ipairs(arpQueue) do
			videochip:DrawText(vec2(MARGIN, y), font, "WHO-HAS " .. entry.dst .. " (from " .. entry.src .. ")", color.magenta, color.black)
			y = y + LINE_HEIGHT
		end
	end

	-- Traffic log
	y = y + LINE_HEIGHT
	videochip:DrawText(vec2(MARGIN, y), font, "-- TRAFFIC LOG --", color.white, color.black)
	y = y + LINE_HEIGHT

	local maxLines = math.floor((videochip.Height - y) / LINE_HEIGHT)
	for i = 1, math.min(maxLines, #logLines) do
		videochip:DrawText(vec2(MARGIN, y), font, logLines[i], color.cyan, color.black)
		y = y + LINE_HEIGHT
	end
end

-- ============================================================
-- UPDATE (runs every tick)
-- ============================================================

function update()
	tickCount = tickCount + 1

	-- Check heartbeat timeouts
	for i = 1, PORT_COUNT do
		local p = ports[i]
		if p.alive and (tickCount - p.lastSeen) > HEARTBEAT_TIMEOUT then
			dbg("TIMEOUT: Device '" .. (p.deviceId or "?") .. "' on P" .. i .. " went offline")
			p.alive = false
			-- Remove from addrTable
			if p.deviceId and addrTable[p.deviceId] == i then
				addrTable[p.deviceId] = nil
			end
			pushLog((p.deviceId or "?") .. " on P" .. i .. " OFFLINE")
		end
	end

	-- Process ARP timeouts
	processArpTimeouts()

	-- LED countdown
	for i = 1, PORT_COUNT do
		if ports[i].ledTimer > 0 then
			ports[i].ledTimer -= 1
			if ports[i].ledTimer == 0 then
				ports[i].led.State = false
			end
		end
	end

	drawUI()
end
