import json
import math
import unittest
from typing import get_args
from unittest.mock import patch

from pydantic import ValidationError
from PIL import Image, ImageFont

import guide_videos as guides
from camera_guide import camera_geometry
from prompts import ANALYSIS_PROMPT
from reel_analyzer import SceneCameraView, SceneSubjectOrientation, VideoAnalysis
from scene_directions import BodyDirection, CameraViewpoint, ConfidenceLevel, camera_pitch
from tests.test_audience_schema import sample_analysis
from tests.test_guide_videos import motion


class CameraObservationTests(unittest.TestCase):
    def test_legacy_analysis_does_not_invent_missing_directions_or_angles(self):
        scene=VideoAnalysis.model_validate(sample_analysis()).scene_details[0]
        self.assertIsNone(scene.camera_view)
        self.assertEqual(scene.subject_orientations,[])

    def test_body_head_and_gaze_are_independent_text_fields_in_saved_analysis(self):
        payload=sample_analysis()
        scene=payload['scene_details'][0]
        scene['subject_orientations']=[{
            'subject_id':'인물 A','facing':'front','body_direction':'왼쪽 사선',
            'head_direction':'정면','gaze_direction':'카메라를 봄','gaze_target':'카메라',
            'confidence':'높음','evidence':'몸 앞면은 화면 왼쪽, 얼굴과 눈은 카메라 쪽으로 보임'}]
        scene['camera_view']={'viewpoint':'약한 하향','confidence':'보통','aim_target':'가슴',
            'evidence':'어깨 윗면과 바닥 원근이 보임'}
        saved=VideoAnalysis.model_validate_json(json.dumps(payload)).model_dump()['scene_details'][0]
        pose=saved['subject_orientations'][0]
        self.assertEqual(pose['body_direction'],'왼쪽 사선')
        self.assertEqual(pose['facing'],'three_quarter')
        self.assertEqual(pose['head_direction'],'정면')
        self.assertEqual(pose['gaze_direction'],'카메라를 봄')
        self.assertEqual(saved['camera_view']['viewpoint'],'약한 하향')
        self.assertFalse(any('degrees' in key or 'yaw' in key or 'pitch' in key for key in (*pose,*saved['camera_view'])))

    def test_cropped_face_can_have_known_body_but_unknown_gaze(self):
        pose=SceneSubjectOrientation(subject_id='인물 A',body_direction='오른쪽 측면')
        self.assertEqual(pose.facing,'side')
        self.assertEqual(pose.gaze_direction,'확인 불가')
        self.assertEqual(pose.head_direction,'확인 불가')
        self.assertIsNone(pose.gaze_target)
        view=SceneCameraView(evidence='원근 근거가 부족함')
        self.assertEqual(view.viewpoint,'확인 불가')
        self.assertEqual(view.confidence,'확인 불가')
        self.assertIsNone(camera_pitch({'camera_viewpoint':'확인 불가'}))

    def test_only_shared_labels_are_accepted_and_both_prompts_cover_them(self):
        for label in get_args(BodyDirection):
            self.assertIn(label,ANALYSIS_PROMPT)
            self.assertIn(label,guides.PROMPT)
            SceneSubjectOrientation(subject_id='A',body_direction=label)
        for label in get_args(CameraViewpoint):
            self.assertIn(label,ANALYSIS_PROMPT)
            self.assertIn(label,guides.PROMPT)
            SceneCameraView(viewpoint=label)
        for label in get_args(ConfidenceLevel): self.assertIn(label,ANALYSIS_PROMPT)
        for kwargs in ({'viewpoint':35},{'viewpoint':'high-ish'},{'confidence':.8},{'confidence':'최고'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValidationError): SceneCameraView(**kwargs)
        with self.assertRaises(ValidationError):
            SceneSubjectOrientation(subject_id='A',body_direction='왼쪽 35도')

    def test_reference_transfer_matches_people_by_id_and_keeps_labels(self):
        plan=motion()
        for scene in plan['scenes']:
            scene['actors']=[dict(scene['actors'][0],subject_id=key) for key in ('인물 B','인물 A')]
        reference={'reference_id':'R1','reel_id':'test_ref','scene_details':[{
            'scene_number':1,'camera':'shot_size=full; angle=high_angle; composition=center',
            'layout_type':'full_body_fashion_shot',
            'camera_view':{'viewpoint':'보통 하향','confidence':'높음','aim_target':'가슴'},
            'subject_orientations':[
                {'subject_id':'인물 A','body_direction':'정면','gaze_direction':'확인 불가'},
                {'subject_id':'인물 B','body_direction':'왼쪽 강한 사선','head_direction':'정면','gaze_direction':'카메라를 봄'}]}]}
        guides.apply_reference_framing(plan,[reference])
        scene=plan['scenes'][0]
        self.assertEqual([actor['body_direction'] for actor in scene['actors']],['왼쪽 강한 사선','정면'])
        self.assertFalse(any('yaw_degrees' in actor for actor in scene['actors']))
        self.assertEqual(scene['actors'][0]['head_direction'],'정면')
        self.assertEqual(scene['actors'][0]['gaze_direction'],'카메라를 봄')
        self.assertEqual(scene['framing']['camera_viewpoint'],'보통 하향')
        self.assertEqual(scene['framing']['aim_target'],'가슴')
        self.assertEqual(scene['camera'],'보통 하향 · 고정 카메라')

    def test_3d_presets_preserve_direction_and_relative_strength_without_numeric_labels(self):
        base=motion()['scenes'][0]['framing']
        def inclination(label):
            geometry=camera_geometry(dict(base,camera_viewpoint=label,aim_target='가슴'),.18,.87)
            delta=[geometry['target'][i]-geometry['camera'][i] for i in range(3)]
            self.assertEqual(geometry['target'][1],.32)
            return math.atan2(delta[1],math.hypot(delta[0],delta[2]))
        self.assertGreater(inclination('강한 하향'),inclination('약한 하향'))
        self.assertGreater(inclination('약한 하향'),inclination('수평'))
        self.assertGreater(inclination('수평'),inclination('약한 상향'))
        scene=motion()['scenes'][0]
        scene['framing']['camera_viewpoint']='강한 하향'
        scene['actors'][0]['body_direction']='왼쪽 강한 사선'
        with patch.object(guides,'draw_mannequin') as render:
            guides.draw_shot(Image.new('RGB',(540,960)),scene,0,ImageFont.load_default())
        self.assertLess(render.call_args.args[3],0)
        self.assertLess(render.call_args.args[4],0)
