from app.datasource.tikhub import VideoMeta, video_meta_to_media_ref, DOUYIN_DOWNLOAD_HEADERS, _parse_video

def test_parse_video_extracts_author_nickname():
    item = {
        "desc": "标题",
        "statistics": {"play_count": 1, "digg_count": 2},
        "video": {"play_addr": {"url_list": ["https://cdn.example/a.mp4"]}},
        "author": {"nickname": "张三"},
    }
    v = _parse_video(item)
    assert v.author == "张三"
    assert v.title == "标题"

def test_video_meta_to_media_ref_fills_headers_title_author():
    v = VideoMeta(
        title="你好", play_count=1, fav_count=2,
        download_url="https://cdn.example/a.mp4", author="张三",
    )
    ref = video_meta_to_media_ref(v)
    assert ref.platform == "douyin"
    assert ref.author == "张三"
    assert ref.title == "你好"
    assert ref.headers["Referer"] == "https://www.douyin.com/"
    assert "User-Agent" in ref.headers
