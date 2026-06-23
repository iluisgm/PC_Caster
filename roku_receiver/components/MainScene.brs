sub init()
    m.video  = m.top.findNode("video")
    m.status = m.top.findNode("status")
    m.video.observeField("state", "onVideoState")
    m.video.setFocus(true)
end sub

sub onLaunchArgs()
    playFromArgs(m.top.launchArgs)
end sub

sub onEcpInput()
    playFromArgs(m.top.ecpInput)
end sub

' Pull a URL out of whatever param the sender used, then play it.
sub playFromArgs(args as Object)
    if args = invalid then return

    url = ""
    if args.contentId <> invalid and args.contentId <> "" then url = args.contentId
    if url = "" and args.url <> invalid then url = args.url
    if url = "" and args.u   <> invalid then url = args.u
    if url = "" then return

    fmt = "hls"
    if args.mediaType <> invalid and args.mediaType <> "" then fmt = args.mediaType
    if args.videoFormat <> invalid and args.videoFormat <> "" then fmt = args.videoFormat

    playUrl(url, fmt)
end sub

sub playUrl(url as String, fmt as String)
    m.status.text = "Loading: " + url

    content = CreateObject("roSGNode", "ContentNode")
    content.url = url
    content.streamFormat = fmt
    content.title = "PC Caster"
    content.playStart = 0

    m.video.content = content
    m.video.control = "play"
end sub

sub onVideoState()
    st = m.video.state
    if st = "playing" then
        m.status.visible = false
    else if st = "error" then
        m.status.visible = true
        m.status.text = "Playback error — check the proxy is running on the PC."
    else
        m.status.text = "Status: " + st
    end if
end sub
