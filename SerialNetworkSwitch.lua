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
local COM_PORTS = { 21, 31, 41, 51 }
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
		deviceId = nil,
		lastSeen = -9999,
		alive = false,
		-- Port statistics
		framesIn = 0,
		framesOut = 0,
		framesDropped = 0,
		bytesIn = 0,
		bytesOut = 0,
	}
	dbg("  Port " .. i .. " -> COM" .. COM_PORTS[i])
end

-- MAC/id learning table: device id string -> port index
local addrTable = {}

-- Rolling log of recent switching decisions, newest first
local logLines = {}
local MAX_LOG_LINES = 12

-- ARP pending queue: list of {dst, srcPort, frame, sentTick}
local arpQueue = {}

-- Global tick counter
local tickCount = 0

-- Traffic visualizer: active animations
-- Each entry: {srcPort, dstPort, progress (0.0-1.0), color}
local trafficAnims = {}
local ANIM_SPEED = 0.05  -- progress per tick (20 ticks = full travel)

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
-- STATS HELPERS
-- ============================================================

local function recordFrameIn(portIdx, line)
	ports[portIdx].framesIn = ports[portIdx].framesIn + 1
	ports[portIdx].bytesIn = ports[portIdx].bytesIn + string.len(line)
end

local function recordFrameOut(portIdx, line)
	ports[portIdx].framesOut = ports[portIdx].framesOut + 1
	ports[portIdx].bytesOut = ports[portIdx].bytesOut + string.len(line)
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
		-- Send ARP to any port with an active serial link, not just alive ones.
		-- A device might not have sent a HELLO yet but can still respond to ARP.
		if i ~= excludePort and ports[i].serial.IsActive then
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
			recordFrameOut(srcPort, entry.frame)
			flashPort(srcPort)
			addTrafficAnim(entry.srcPort, srcPort, color.green)
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
			dbg("ARP: TIMEOUT for " .. entry.dst .. " — fallback flooding frame from " .. entry.src)
			-- Fallback: flood to all active ports instead of dropping
			local flooded = false
			for i = 1, PORT_COUNT do
				if i ~= entry.srcPort and ports[i].serial.IsActive then
					ports[i].serial:Println(entry.frame)
					recordFrameOut(i, entry.frame)
					flashPort(i)
					addTrafficAnim(entry.srcPort, i, color.yellow)
					flooded = true
				end
			end
			if flooded then
				pushLog(entry.src .. " -> " .. entry.dst .. "  (ARP timeout, flooded)")
			else
				recordFrameDrop(entry.srcPort)
				pushLog(entry.src .. " -> " .. entry.dst .. "  (ARP timeout, no ports)")
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
	-- Parse DATA:<src>:<dst>:<msg>
	local src, dst, msg = string.match(line, "^DATA:(.-):(.-):(.*)")
	if src == nil or dst == nil then
		dbg("BAD DATA FRAME on P" .. srcPort .. ": " .. line)
		pushLog("(bad frame on P" .. srcPort .. ")")
		recordFrameDrop(srcPort)
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
	recordFrameIn(srcPort, line)
	flashPort(srcPort)

	-- Broadcast
	if dst == "ALL" then
		dbg("  -> Broadcasting to all other ports")
		for i = 1, PORT_COUNT do
			if i ~= srcPort then
				ports[i].serial:Println(line)
				recordFrameOut(i, line)
				flashPort(i)
				addTrafficAnim(srcPort, i, color.yellow)
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
		recordFrameOut(knownPort, line)
		flashPort(knownPort)
		addTrafficAnim(srcPort, knownPort, color.green)
		pushLog(src .. " -> " .. dst .. "  (fwd P" .. knownPort .. ")")
		return
	end

	-- Unknown destination: ARP instead of blind flood
	dbg("  -> Unknown dst '" .. dst .. "', sending ARP WHO-HAS")
	sendArpRequest(dst, srcPort)
	addTrafficAnim(srcPort, 0, color.magenta)  -- 0 = "to switch center" for ARP visual

	-- Buffer the frame while waiting for ARP reply
	table.insert(arpQueue, {
		dst = dst,
		src = src,
		srcPort = srcPort,
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

	-- HELLO:<id> — handle silently (no debug log, too frequent)
	local helloId = string.match(line, "^HELLO:(.*)")
	if helloId then
		handleHello(srcPort, trim(helloId))
		return
	end

	dbg("RX P" .. srcPort .. ": " .. line)

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

-- Format byte count nicely
local function fmtBytes(b)
	if b >= 1048576 then
		return string.format("%.1fMB", b / 1048576)
	elseif b >= 1024 then
		return string.format("%.1fKB", b / 1024)
	else
		return tostring(b) .. "B"
	end
end

local function drawTrafficVisualizer(x, y, w, h)
	-- Draw a central switch box with ports around it
	-- Layout: ports on left side, switch box in center
	local centerX = x + math.floor(w / 2)
	local centerY = y + math.floor(h / 2)
	local switchSize = 12

	-- Draw switch box in center
	videochip:DrawRect(
		vec2(centerX - switchSize, centerY - switchSize),
		vec2(centerX + switchSize, centerY + switchSize),
		color.white
	)
	videochip:DrawText(vec2(centerX - 5, centerY - 3), font, "SW", color.white, color.black)

	-- Port positions (arranged around the switch)
	local portPositions = {}
	local radius = math.min(w, h) / 2 - 8
	for i = 1, PORT_COUNT do
		local angle = (i - 1) * (2 * math.pi / PORT_COUNT) - math.pi / 2
		local px = centerX + math.floor(math.cos(angle) * radius)
		local py = centerY + math.floor(math.sin(angle) * radius)
		portPositions[i] = { x = px, y = py }

		-- Port dot
		local dotColor = color.red
		if ports[i].alive then
			dotColor = color.green
		elseif ports[i].serial.IsActive then
			dotColor = color.yellow
		end
		videochip:DrawCircle(vec2(px, py), 3, dotColor)

		-- Port label
		local labelX = px - 3
		local labelY = py + 4
		if py < centerY then labelY = py - 10 end
		videochip:DrawText(vec2(labelX, labelY), font, "P" .. i, color.gray, color.black)

		-- Static line from port to switch center
		videochip:DrawLine(vec2(px, py), vec2(centerX, centerY), color.gray)
	end

	-- Draw animated packets
	for _, anim in ipairs(trafficAnims) do
		local srcPos = portPositions[anim.srcPort]
		local dstPos
		if anim.dstPort == 0 then
			-- ARP: animate toward center
			dstPos = { x = centerX, y = centerY }
		elseif portPositions[anim.dstPort] then
			dstPos = portPositions[anim.dstPort]
		else
			dstPos = { x = centerX, y = centerY }
		end

		if srcPos and dstPos then
			-- Interpolate position
			local px = srcPos.x + math.floor((dstPos.x - srcPos.x) * anim.progress)
			local py = srcPos.y + math.floor((dstPos.y - srcPos.y) * anim.progress)
			videochip:DrawCircle(vec2(px, py), 2, anim.color)
		end
	end
end

local function drawUI()
	videochip:Clear(color.black)

	local screenW = videochip.Width
	local screenH = videochip.Height

	-- Layout: left panel (text info), right panel (traffic visualizer)
	local vizWidth = math.min(100, math.floor(screenW * 0.4))
	local textWidth = screenW - vizWidth - MARGIN

	-- ===== RIGHT PANEL: Traffic Visualizer =====
	local vizX = screenW - vizWidth
	drawTrafficVisualizer(vizX, MARGIN, vizWidth - MARGIN, screenH - MARGIN * 2)

	-- ===== LEFT PANEL: Text info =====
	local y = MARGIN

	-- Header
	videochip:DrawText(vec2(MARGIN, y), font, "SERIAL SWITCH", color.white, color.black)
	y = y + LINE_HEIGHT + 2

	-- Port status with stats
	videochip:DrawText(vec2(MARGIN, y), font, "PORT STATUS", color.white, color.black)
	y = y + LINE_HEIGHT

	for i = 1, PORT_COUNT do
		local p = ports[i]
		local statusText
		local statusColor

		if p.alive then
			statusText = "ON"
			statusColor = color.green
		elseif p.serial.IsActive then
			statusText = "LK"
			statusColor = color.yellow
		else
			statusText = "--"
			statusColor = color.red
		end

		local devName = p.deviceId or "---"
		local statsStr = string.format("%dI/%dO", p.framesIn, p.framesOut)
		if p.framesDropped > 0 then
			statsStr = statsStr .. "/" .. p.framesDropped .. "D"
		end

		videochip:DrawText(vec2(MARGIN, y), font, "P" .. i, color.white, color.black)
		videochip:DrawText(vec2(MARGIN + 12, y), font, statusText, statusColor, color.black)
		videochip:DrawText(vec2(MARGIN + 28, y), font, devName, color.gray, color.black)
		videochip:DrawText(vec2(MARGIN + 60, y), font, statsStr, color.gray, color.black)
		y = y + LINE_HEIGHT
	end

	-- Port byte totals
	y = y + 2
	videochip:DrawText(vec2(MARGIN, y), font, "THROUGHPUT", color.white, color.black)
	y = y + LINE_HEIGHT
	for i = 1, PORT_COUNT do
		local p = ports[i]
		local throughStr = "P" .. i .. " " .. fmtBytes(p.bytesIn) .. " in / " .. fmtBytes(p.bytesOut) .. " out"
		videochip:DrawText(vec2(MARGIN, y), font, throughStr, color.gray, color.black)
		y = y + LINE_HEIGHT
	end

	-- MAC table (compact)
	y = y + 2
	videochip:DrawText(vec2(MARGIN, y), font, "MAC TABLE", color.yellow, color.black)
	y = y + LINE_HEIGHT

	local hasEntries = false
	local macStr = ""
	for id, port in pairs(addrTable) do
		hasEntries = true
		if macStr ~= "" then macStr = macStr .. " | " end
		macStr = macStr .. id .. ":P" .. port
	end
	if hasEntries then
		-- Wrap if too long
		if string.len(macStr) > math.floor(textWidth / 4) then
			-- Split into multiple lines
			for id, port in pairs(addrTable) do
				videochip:DrawText(vec2(MARGIN, y), font, id .. " -> P" .. port, color.yellow, color.black)
				y = y + LINE_HEIGHT
			end
		else
			videochip:DrawText(vec2(MARGIN, y), font, macStr, color.yellow, color.black)
			y = y + LINE_HEIGHT
		end
	else
		videochip:DrawText(vec2(MARGIN, y), font, "(empty)", color.gray, color.black)
		y = y + LINE_HEIGHT
	end

	-- ARP queue (if any)
	if #arpQueue > 0 then
		y = y + 2
		videochip:DrawText(vec2(MARGIN, y), font, "ARP PENDING", color.magenta, color.black)
		y = y + LINE_HEIGHT
		for _, entry in ipairs(arpQueue) do
			videochip:DrawText(vec2(MARGIN, y), font, "? " .. entry.dst .. " <-" .. entry.src, color.magenta, color.black)
			y = y + LINE_HEIGHT
		end
	end

	-- Traffic log (fill remaining space)
	y = y + 2
	videochip:DrawText(vec2(MARGIN, y), font, "LOG", color.white, color.black)
	y = y + LINE_HEIGHT

	local maxLines = math.floor((screenH - y) / LINE_HEIGHT)
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
			if p.deviceId and addrTable[p.deviceId] == i then
				addrTable[p.deviceId] = nil
			end
			pushLog((p.deviceId or "?") .. " on P" .. i .. " OFFLINE")
		end

		-- Diagnostic: warn about ports stuck at LINK (never received any data)
		-- This usually means the event channel isn't wired in the Multitool
		if not p.alive and p.serial.IsActive and p.lastSeen == -9999 then
			if tickCount == 600 then  -- warn once at ~10 seconds
				dbg("WARNING: P" .. i .. " has LINK but never received data. Is event channel " .. i .. " wired to Serial" .. (i-1) .. "?")
				pushLog("P" .. i .. " LINK but no data — check wiring!")
			end
		end
	end

	-- Process ARP timeouts
	processArpTimeouts()

	-- Update traffic animations
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
