-- ============================================================
-- SERIAL NETWORK SWITCH — Retro Gadgets
-- ============================================================
-- Build requirements for this gadget:
--   4x Serial      (gdt.Serial0 .. gdt.Serial3)
--   4x Led         (gdt.Led0 .. gdt.Led3)
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
--   HELLO:<id>                  — device heartbeat/announce
--   ARP:WHO-HAS:<id>           — switch asks "who is <id>?"
--   ARP:IS-AT:<id>             — device replies "I am <id>"
--   DATA:<src>:<dst>:<seq>:<msg> — payload frame with sequence number
--   DATA:<src>:<dst>:<msg>      — legacy payload frame (no seq)
--   "ALL" as dst means broadcast
-- ============================================================

local PORT_COUNT = 4

-- ---- EDIT THESE to match your com0com/socat virtual port numbers ----
local COM_PORTS = { 21, 31, 41, 51 }
-- -----------------------------------------------------------------

local HEARTBEAT_TIMEOUT = 300  -- ~5 seconds at 60fps
local ARP_TIMEOUT = 60         -- ~1 second at 60fps

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

local function dbg(msg)
	print("[SWITCH] " .. msg)
end

-- ============================================================
-- INIT
-- ============================================================

local videochip = gdt.VideoChip0
local font = gdt.ROM.System.SpriteSheets["StandardFont"]

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
		deviceId = nil,
		lastSeen = -9999,
		alive = false,
		framesIn = 0,
		framesOut = 0,
		framesDropped = 0,
	}
	dbg("  Port " .. i .. " -> COM" .. COM_PORTS[i])
end

local addrTable = {}
local logLines = {}
local MAX_LOG_LINES = 8
local arpQueue = {}
local tickCount = 0

-- Traffic visualizer animations
local trafficAnims = {}
local ANIM_SPEED = 0.04

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
-- TRAFFIC VISUALIZER
-- ============================================================

local function addTrafficAnim(srcPort, dstPort, animColor)
	table.insert(trafficAnims, {
		srcPort = srcPort,
		dstPort = dstPort,
		progress = 0.0,
		color = animColor or color.cyan,
	})
end

local function updateTrafficAnims()
	local remaining = {}
	for _, anim in ipairs(trafficAnims) do
		anim.progress = anim.progress + ANIM_SPEED
		if anim.progress < 1.0 then
			table.insert(remaining, anim)
		end
	end
	trafficAnims = remaining
end

-- ============================================================
-- STATS
-- ============================================================

local function recordFrameIn(portIdx)
	ports[portIdx].framesIn = ports[portIdx].framesIn + 1
end

local function recordFrameOut(portIdx)
	ports[portIdx].framesOut = ports[portIdx].framesOut + 1
end

local function recordFrameDrop(portIdx)
	ports[portIdx].framesDropped = ports[portIdx].framesDropped + 1
end

-- ============================================================
-- ARP LOGIC
-- ============================================================

local function sendArpRequest(dst, excludePort)
	dbg("ARP: WHO-HAS " .. dst .. " (excluding P" .. excludePort .. ")")
	for i = 1, PORT_COUNT do
		if i ~= excludePort and ports[i].serial.IsActive then
			ports[i].serial:Println("ARP:WHO-HAS:" .. dst)
			flashPort(i)
		end
	end
end

local function handleArpReply(srcPort, id)
	dbg("ARP: IS-AT " .. id .. " on P" .. srcPort)
	addrTable[id] = srcPort
	pushLog(id .. " found at P" .. srcPort)

	local remaining = {}
	for _, entry in ipairs(arpQueue) do
		if entry.dst == id then
			dbg("ARP: Delivering buffered frame to " .. id .. " via P" .. srcPort)
			ports[srcPort].serial:Println(entry.frame)
			recordFrameOut(srcPort)
			flashPort(srcPort)
			addTrafficAnim(entry.srcPort, srcPort, color.green)
			pushLog(entry.src .. " > " .. id .. " (delivered)")
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
			dbg("ARP: TIMEOUT for " .. entry.dst .. " — fallback flooding")
			local flooded = false
			for i = 1, PORT_COUNT do
				if i ~= entry.srcPort and ports[i].serial.IsActive then
					ports[i].serial:Println(entry.frame)
					recordFrameOut(i)
					flashPort(i)
					addTrafficAnim(entry.srcPort, i, color.yellow)
					flooded = true
				end
			end
			if flooded then
				pushLog(entry.src .. " > " .. entry.dst .. " (flood)")
			else
				recordFrameDrop(entry.srcPort)
				pushLog(entry.src .. " > " .. entry.dst .. " (DROPPED)")
			end
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
	ports[srcPort].deviceId = id
	ports[srcPort].lastSeen = tickCount
	ports[srcPort].alive = true
	addrTable[id] = srcPort
end

local function handleDataFrame(srcPort, line)
	-- Try new format: DATA:<src>:<dst>:<seq>:<msg>
	local src, dst, seq, msg = string.match(line, "^DATA:(.-):(.-):(.-):(.*)$")

	if src == nil or dst == nil then
		dbg("BAD DATA FRAME on P" .. srcPort .. ": " .. line)
		pushLog("bad frame P" .. srcPort)
		recordFrameDrop(srcPort)
		return
	end

	src = trim(src)
	dst = trim(dst)

	-- If seq looks like a number, it's the new format. Otherwise it's part of the message.
	if seq and not tonumber(seq) then
		-- Legacy format: what we parsed as "seq" is actually start of msg
		msg = seq .. ":" .. msg
	end

	dbg("DATA " .. src .. " > " .. dst .. " on P" .. srcPort)

	-- Learn source
	addrTable[src] = srcPort
	ports[srcPort].deviceId = src
	ports[srcPort].lastSeen = tickCount
	ports[srcPort].alive = true
	recordFrameIn(srcPort)
	flashPort(srcPort)

	-- Broadcast
	if dst == "ALL" then
		for i = 1, PORT_COUNT do
			if i ~= srcPort then
				ports[i].serial:Println(line)
				recordFrameOut(i)
				flashPort(i)
				addTrafficAnim(srcPort, i, color.yellow)
			end
		end
		pushLog(src .. " > ALL (broadcast)")
		return
	end

	-- Known destination
	local knownPort = addrTable[dst]
	if knownPort ~= nil and knownPort ~= srcPort then
		ports[knownPort].serial:Println(line)
		recordFrameOut(knownPort)
		flashPort(knownPort)
		addTrafficAnim(srcPort, knownPort, color.green)
		pushLog(src .. " > " .. dst .. " P" .. knownPort)
		return
	end

	-- Unknown: ARP
	dbg("  Unknown dst '" .. dst .. "', sending ARP")
	sendArpRequest(dst, srcPort)
	addTrafficAnim(srcPort, 0, color.magenta)

	table.insert(arpQueue, {
		dst = dst,
		src = src,
		srcPort = srcPort,
		frame = line,
		sentTick = tickCount,
	})
	pushLog(src .. " > " .. dst .. " (ARP...)")
end

-- ============================================================
-- INCOMING LINE DISPATCHER
-- ============================================================

local function handleLine(srcPort, rawLine)
	local line = trim(rawLine)
	if line == nil or line == "" then return end

	-- HELLO
	local helloId = string.match(line, "^HELLO:(.*)")
	if helloId then
		handleHello(srcPort, trim(helloId))
		return
	end

	dbg("RX P" .. srcPort .. ": " .. line)

	-- ARP:IS-AT
	local arpId = string.match(line, "^ARP:IS%-AT:(.*)")
	if arpId then
		handleArpReply(srcPort, trim(arpId))
		return
	end

	-- DATA frame
	if string.sub(line, 1, 5) == "DATA:" then
		handleDataFrame(srcPort, line)
		return
	end

	-- Legacy format
	local src, dst, msg = string.match(line, "^(.-):(.-):(.*)$")
	if src and dst then
		dbg("  (legacy format)")
		handleDataFrame(srcPort, "DATA:" .. line)
		return
	end

	dbg("  UNKNOWN: " .. line)
end

-- ============================================================
-- EVENT CHANNELS
-- ============================================================

function eventChannel1(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do handleLine(1, line) end
	end
end

function eventChannel2(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do handleLine(2, line) end
	end
end

function eventChannel3(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do handleLine(3, line) end
	end
end

function eventChannel4(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do handleLine(4, line) end
	end
end

-- ============================================================
-- DISPLAY — Clean, readable layout
-- ============================================================

local LH = 10   -- line height (slightly more breathing room)
local M = 5     -- margin

local function drawHLine(y, c)
	videochip:DrawLine(vec2(M, y), vec2(videochip.Width - M, y), c or color.gray)
end

local function drawTrafficVisualizer(x, y, w, h)
	local centerX = x + math.floor(w / 2)
	local centerY = y + math.floor(h / 2)
	local radius = math.min(w, h) / 2 - 12

	-- Switch center
	videochip:DrawRect(
		vec2(centerX - 8, centerY - 8),
		vec2(centerX + 8, centerY + 8),
		color.white
	)

	-- Port nodes
	local portPos = {}
	for i = 1, PORT_COUNT do
		local angle = (i - 1) * (2 * math.pi / PORT_COUNT) - math.pi / 2
		local px = centerX + math.floor(math.cos(angle) * radius)
		local py = centerY + math.floor(math.sin(angle) * radius)
		portPos[i] = { x = px, y = py }

		-- Connection line
		videochip:DrawLine(vec2(px, py), vec2(centerX, centerY), ColorRGBA(40, 40, 40, 255))

		-- Port node (larger, colored by status)
		local nodeColor = color.red
		if ports[i].alive then
			nodeColor = color.green
		elseif ports[i].serial.IsActive then
			nodeColor = color.yellow
		end
		videochip:DrawCircle(vec2(px, py), 4, nodeColor)

		-- Label
		local lx = px + (px > centerX and 5 or -12)
		local ly = py + (py > centerY and 5 or -10)
		videochip:DrawText(vec2(lx, ly), font, "P" .. i, color.white, color.black)
	end

	-- Animated packets
	for _, anim in ipairs(trafficAnims) do
		local srcPos = portPos[anim.srcPort]
		local dstPos
		if anim.dstPort == 0 then
			dstPos = { x = centerX, y = centerY }
		elseif portPos[anim.dstPort] then
			dstPos = portPos[anim.dstPort]
		else
			dstPos = { x = centerX, y = centerY }
		end

		if srcPos and dstPos then
			local px = srcPos.x + math.floor((dstPos.x - srcPos.x) * anim.progress)
			local py = srcPos.y + math.floor((dstPos.y - srcPos.y) * anim.progress)
			videochip:DrawCircle(vec2(px, py), 3, anim.color)
		end
	end
end

local function drawUI()
	videochip:Clear(color.black)

	local W = videochip.Width
	local H = videochip.Height

	-- Split: left 55% for info, right 45% for visualizer
	local splitX = math.floor(W * 0.55)

	-- ===== RIGHT: Traffic Visualizer =====
	drawTrafficVisualizer(splitX, 0, W - splitX, H)

	-- Vertical divider
	videochip:DrawLine(vec2(splitX - 2, 0), vec2(splitX - 2, H), ColorRGBA(50, 50, 50, 255))

	-- ===== LEFT: Info panels =====
	local y = M

	-- Title
	videochip:DrawText(vec2(M, y), font, "NETWORK SWITCH", color.white, color.black)
	y = y + LH + 2
	drawHLine(y, ColorRGBA(60, 60, 60, 255))
	y = y + 4

	-- Port status table
	for i = 1, PORT_COUNT do
		local p = ports[i]
		local statusChar, statusColor

		if p.alive then
			statusChar = "[ON]"
			statusColor = color.green
		elseif p.serial.IsActive then
			statusChar = "[LK]"
			statusColor = color.yellow
		else
			statusChar = "[--]"
			statusColor = color.red
		end

		local name = p.deviceId or "---"

		-- Port line: P1 [ON] PC1    12>  8<  0x
		videochip:DrawText(vec2(M, y), font, "P" .. i, color.white, color.black)
		videochip:DrawText(vec2(M + 14, y), font, statusChar, statusColor, color.black)
		videochip:DrawText(vec2(M + 38, y), font, name, color.white, color.black)

		-- Compact stats
		local stats = p.framesIn .. ">" .. p.framesOut .. "<"
		if p.framesDropped > 0 then
			stats = stats .. p.framesDropped .. "x"
			videochip:DrawText(vec2(M + 72, y), font, stats, color.red, color.black)
		else
			videochip:DrawText(vec2(M + 72, y), font, stats, color.gray, color.black)
		end

		y = y + LH
	end

	y = y + 3
	drawHLine(y, ColorRGBA(60, 60, 60, 255))
	y = y + 4

	-- MAC Table (compact, single line per entry)
	videochip:DrawText(vec2(M, y), font, "MAC TABLE", color.yellow, color.black)
	y = y + LH

	local macCount = 0
	for id, port in pairs(addrTable) do
		macCount = macCount + 1
		if macCount <= 4 then
			videochip:DrawText(vec2(M, y), font, " " .. id .. " > P" .. port, color.yellow, color.black)
			y = y + LH
		end
	end
	if macCount == 0 then
		videochip:DrawText(vec2(M, y), font, " (empty)", color.gray, color.black)
		y = y + LH
	elseif macCount > 4 then
		videochip:DrawText(vec2(M, y), font, " +" .. (macCount - 4) .. " more", color.gray, color.black)
		y = y + LH
	end

	-- ARP pending (only show if any)
	if #arpQueue > 0 then
		y = y + 2
		videochip:DrawText(vec2(M, y), font, "ARP (" .. #arpQueue .. " pending)", color.magenta, color.black)
		y = y + LH
	end

	y = y + 3
	drawHLine(y, ColorRGBA(60, 60, 60, 255))
	y = y + 4

	-- Traffic log (fill remaining space)
	videochip:DrawText(vec2(M, y), font, "LOG", color.white, color.black)
	y = y + LH

	local maxLines = math.floor((H - y - 2) / LH)
	for i = 1, math.min(maxLines, #logLines) do
		videochip:DrawText(vec2(M, y), font, logLines[i], color.cyan, color.black)
		y = y + LH
	end
end

-- ============================================================
-- UPDATE
-- ============================================================

function update()
	tickCount = tickCount + 1

	-- Heartbeat timeouts
	for i = 1, PORT_COUNT do
		local p = ports[i]
		if p.alive and (tickCount - p.lastSeen) > HEARTBEAT_TIMEOUT then
			dbg("TIMEOUT: '" .. (p.deviceId or "?") .. "' on P" .. i)
			p.alive = false
			if p.deviceId and addrTable[p.deviceId] == i then
				addrTable[p.deviceId] = nil
			end
			pushLog((p.deviceId or "?") .. " P" .. i .. " OFFLINE")
		end

		-- Wiring diagnostic
		if not p.alive and p.serial.IsActive and p.lastSeen == -9999 then
			if tickCount == 600 then
				dbg("WARNING: P" .. i .. " has LINK but no data. Check event channel " .. i .. " wiring!")
				pushLog("P" .. i .. " no data - check wiring!")
			end
		end
	end

	-- ARP timeouts
	processArpTimeouts()

	-- Traffic animations
	updateTrafficAnims()

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
