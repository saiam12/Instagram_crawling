"""Canonical textual observations; numbers below are diagram presets, not measurements."""
from typing import Literal

BodyDirection = Literal['정면','왼쪽 약한 사선','왼쪽 사선','왼쪽 강한 사선','왼쪽 측면','왼쪽 뒤 사선',
    '후면','오른쪽 뒤 사선','오른쪽 측면','오른쪽 강한 사선','오른쪽 사선','오른쪽 약한 사선','확인 불가']
CameraViewpoint = Literal['수평','아주 약한 하향','약한 하향','보통 하향','강한 하향','매우 강한 하향','수직 하향',
    '아주 약한 상향','약한 상향','보통 상향','강한 상향','매우 강한 상향','수직 상향','확인 불가']
GazeDirection = Literal['카메라를 봄','화면 왼쪽','화면 오른쪽','위쪽','아래쪽','카메라 반대쪽','기타','확인 불가']
ConfidenceLevel = Literal['매우 높음','높음','보통','낮음','확인 불가']
AimTarget = Literal['얼굴','가슴','배·허리','하반신','발','전신 중심','상품','기타','확인 불가']

# These presets only choose a consistent illustrative 3D pose for a text label.
BODY_YAW = {'정면':0,'왼쪽 약한 사선':-20,'왼쪽 사선':-40,'왼쪽 강한 사선':-65,'왼쪽 측면':-90,
    '왼쪽 뒤 사선':-135,'후면':180,'오른쪽 뒤 사선':135,'오른쪽 측면':90,'오른쪽 강한 사선':65,'오른쪽 사선':40,'오른쪽 약한 사선':20}
CAMERA_PITCH = {'수평':0,'아주 약한 하향':8,'약한 하향':18,'보통 하향':35,'강한 하향':55,'매우 강한 하향':75,'수직 하향':90,
    '아주 약한 상향':-8,'약한 상향':-18,'보통 상향':-35,'강한 상향':-55,'매우 강한 상향':-75,'수직 상향':-90}
AIM_HEIGHT = {'얼굴':.08,'가슴':.32,'배·허리':.57,'하반신':.8,'발':1,'전신 중심':.5}


def coarse_facing(direction):
    if direction=='정면': return 'front'
    if direction=='후면' or '뒤 사선' in direction: return 'back'
    if '측면' in direction: return 'side'
    return 'three_quarter' if '사선' in direction else 'unknown'


def actor_yaw(actor):
    if 'body_direction' in actor: return BODY_YAW.get(actor['body_direction'],25)
    return actor.get('yaw_degrees',25)


def camera_pitch(framing):
    if 'camera_viewpoint' in framing: return CAMERA_PITCH.get(framing['camera_viewpoint'])
    return framing.get('camera_pitch_degrees')
