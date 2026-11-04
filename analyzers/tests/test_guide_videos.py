import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import guide_videos as guides

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from collectors.monitor.backend import recommendations


def motion():
    return {'title':'촬영 테스트','duration_seconds':5,'reference_ids':['R1'],'features':['R1의 전신 구도'],
            'scenes':[{'start_second':start,'end_second':end,'shot':'전신 정면','camera':'고정',
                       'action':'모델이 오른쪽으로 이동','caption':'제품 소개','dialogue':'이 제품을 소개합니다.',
                       'framing':{'shot_size':'full_body','angle':'eye_level','aspect':'portrait','movement':'static','reference_id':'R1','source_scene':1},
                       'actors':[{'label':'모델','gesture':'walk','x':.2,'y':.8,'end_x':.7,'end_y':.8}],
                       'products':[{'label':'상품','x':.7,'y':.4,'end_x':.7,'end_y':.4}]}
                      for start,end in [(0,2),(2,4),(4,5)]]}


class GuideVideoTests(unittest.TestCase):
    def test_camera_diagram_includes_both_people_and_aims_at_group_center(self):
        from PIL import Image,ImageFont
        import camera_guide
        scene=motion()['scenes'][0]
        scene['actors']=[dict(scene['actors'][0],x=x,end_x=x) for x in (.25,.75)]
        framing,_,top,span=guides.shot_frame(scene,0)
        poses=[guides.actor_pose(scene,actor,0) for actor in scene['actors']]
        with patch.object(camera_guide,'draw_mannequin') as render:
            geometry=camera_guide.draw_camera_guide(Image.new('RGB',(540,960)),scene,0,ImageFont.load_default(),framing,top,span,poses)
        self.assertEqual(render.call_count,2)
        self.assertAlmostEqual(geometry['target'][0],.65)

    def test_camera_3d_geometry_points_at_the_target_from_the_right_height(self):
        from camera_guide import camera_geometry
        base=motion()['scenes'][0]['framing']
        for angle,sign in (('high_angle',-1),('eye_level',0),('low_angle',1),('overhead',-1)):
            geometry=camera_geometry(dict(base,angle=angle,aim_height=.5),.18,.87)
            difference=geometry['camera'][1]-geometry['target'][1]
            self.assertEqual(0 if difference==0 else (1 if difference>0 else -1),sign)
            self.assertEqual(geometry['target'][1],.5)
            direction=[geometry['target'][i]-geometry['lens'][i] for i in range(3)]
            self.assertLess(sum(v*v for v in __import__('camera_guide').cross(direction,geometry['forward'])),1e-10)
            self.assertEqual(len(geometry['corners']),4)
        plan=motion(); plan['scenes'][0]['framing']['aim_height']=float('inf')
        with self.assertRaises(ValueError): guides.validate(plan,{'R1'})

    def test_body_orientation_is_independent_of_camera_angle(self):
        import math
        from PIL import Image,ImageFont
        scene=motion()['scenes'][0]
        scene['framing']['angle']='side'
        scene['actors'][0]['yaw_degrees']=35
        with patch.object(guides,'draw_mannequin') as renderer:
            guides.draw_shot(Image.new('RGB',(540,960)),scene,0,ImageFont.load_default())
        self.assertAlmostEqual(renderer.call_args.args[3],math.radians(35))
        scene['actors'][0]['yaw_degrees']=float('nan')
        with self.assertRaises(ValueError): guides.validate({**motion(),'scenes':[scene]+motion()['scenes'][1:]},{'R1'})

    def test_uploaded_orientation_applies_only_to_the_matching_scene(self):
        plan=motion()
        for i,scene in enumerate(plan['scenes']): scene['framing']['source_scene']=i+1
        reference={'reference_id':'R1','reel_id':'DeliKNPprGK','scene_details':[
            {'scene_number':i,'camera':'shot_size=full; angle=high_angle; composition=center','layout_type':'full_body_fashion_shot'} for i in (1,2,3)]}
        guides.apply_reference_framing(plan,[reference])
        self.assertNotIn('yaw_degrees',plan['scenes'][0]['actors'][0])
        self.assertEqual(plan['scenes'][1]['actors'][0]['yaw_degrees'],35)
        self.assertNotIn('yaw_degrees',plan['scenes'][2]['actors'][0])

    def test_solid_mannequin_has_side_depth_and_distinct_back(self):
        import math
        from PIL import Image,ImageDraw,ImageChops
        from mannequin import draw_mannequin
        pose=guides.actor_pose(motion()['scenes'][0],{'gesture':'idle'},0)
        frames=[]
        for yaw in (0,math.pi/2,math.pi):
            image=Image.new('RGB',(300,500),'white')
            draw_mannequin(ImageDraw.Draw(image),(150,20),(450,440),yaw,0,pose,0)
            frames.append(image)
        widths=[]
        for image in frames:
            box=ImageChops.difference(image,Image.new('RGB',image.size,'white')).getbbox()
            widths.append(box[2]-box[0])
        self.assertGreater(widths[1],widths[0]*.3)
        self.assertLess(widths[1],widths[0]*.8)
        self.assertIsNotNone(ImageChops.difference(frames[0],frames[2]).getbbox())

    def test_setup_shows_off_camera_body_without_changing_source_crop(self):
        from PIL import Image,ImageFont
        scene=motion()['scenes'][0]
        scene['framing'].update(shot_size='torso',aspect='landscape',body_crop=[.23,.55])
        scene['actors'][0].update(x=.5,end_x=.5,y=.5,end_y=.5)
        scene['products']=[]
        original=json.dumps(scene,sort_keys=True)
        image=Image.new('RGB',(guides.WIDTH,guides.HEIGHT),'white')
        guides.draw_setup(image,scene,.5,ImageFont.load_default())
        # Legs outside the torso capture remain visible in the context stage.
        self.assertNotEqual(image.getpixel((250,490)),(255,255,255))
        self.assertEqual(json.dumps(scene,sort_keys=True),original)

    def test_camera_follows_each_source_scene_and_face_rule_keeps_legs(self):
        plan=motion()
        sources=[{'scene_number':1,'camera':'shot_size=medium_full; angle=eye_level; composition=mirror_selfie','layout_type':'mirror_selfie'},
                 {'scene_number':2,'camera':'shot_size=close_up; angle=eye_level; composition=center','layout_type':'closeup','visual_description':'소매 디테일'},
                 {'scene_number':3,'camera':'shot_size=medium_close_up; angle=high_angle; composition=center','layout_type':'product_shot','visual_description':'매장 바구니'}]
        for i,scene in enumerate(plan['scenes']): scene['framing']['source_scene']=i+1
        guides.apply_reference_framing(plan,[{'reference_id':'R1','reel_id':'test_ref','scene_details':sources}],{'production_constraints':'얼굴 노출 없이'})
        first,detail,product=plan['scenes']
        self.assertEqual(first['framing']['shot_size'],'medium_full')
        self.assertEqual(first['framing']['body_crop'],[.18,.9])
        self.assertEqual(first['framing']['layout_type'],'mirror_selfie')
        self.assertEqual(detail['framing']['shot_size'],'close_up')
        self.assertEqual(product['framing']['subject_kind'],'basket')
        self.assertEqual(product['framing']['angle'],'high_angle')

    def test_reviewed_frames_are_scoped_by_reel_and_scene(self):
        evidence=json.loads(guides.FRAMING_EVIDENCE.read_text(encoding='utf-8'))
        self.assertGreaterEqual(sum(len(ref['scenes']) for ref in evidence.values()),92)
        r3=evidence['Ddv2UERyS6h']['scenes']
        self.assertGreaterEqual(r3['3']['body_crop'][1],.85)
        self.assertEqual(r3['9']['subject_kind'],'sleeve')
        self.assertEqual(evidence['DeCqQTBJgSC']['scenes']['1']['aspect'],'landscape')
        self.assertEqual(r3['1']['aspect'],'portrait')
        self.assertEqual(r3['1']['subject_kind'],'basket')

    def test_stationary_actor_animates_hands_inside_torso_viewport(self):
        from PIL import Image,ImageFont,ImageChops
        scene=motion()['scenes'][0]
        scene['framing'].update(shot_size='torso')
        scene['actors'][0].update(x=.5,end_x=.5,y=.5,end_y=.5,gesture='present')
        scene['products']=[]
        frames=[]
        for progress in (0,.5):
            image=Image.new('RGB',(guides.WIDTH,guides.HEIGHT),'white')
            guides.draw_shot(image,scene,progress,ImageFont.load_default())
            frames.append(image)
        box=guides.shot_frame(scene,0)[1]
        self.assertIsNotNone(ImageChops.difference(frames[0].crop(box),frames[1].crop(box)).getbbox())
        skin_pixels=frames[1].crop(box).getcolors(box[2]*box[3])
        skin_pixels=sum(count for count,color in skin_pixels if color[0]>color[1]>color[2] and color[0]-color[2]>25)
        self.assertGreater(skin_pixels,20)  # Hands remain visible after torso zoom.

    def test_legacy_zipper_action_moves_hand_without_changing_actor_position(self):
        scene=motion()['scenes'][0]; scene['action']='지퍼를 아래에서 위로 올린다'
        actor=scene['actors'][0]; del actor['gesture']
        start=guides.actor_pose(scene,actor,0); end=guides.actor_pose(scene,actor,1)
        self.assertEqual(start['gesture'],'zip_up')
        self.assertLess(end['right'][1],start['right'][1])
        actor['gesture']='unsupported'
        plan=motion(); plan['scenes'][0]=scene
        with self.assertRaises(ValueError): guides.validate(plan,{'R1'})

    def test_torso_crop_excludes_head_and_legs_and_preserves_picture_aspect(self):
        scene=motion()['scenes'][0]
        scene['framing'].update(shot_size='torso',aspect='landscape')
        _,box,top,span=guides.shot_frame(scene,0)
        self.assertGreater(top,.15)  # head bottom
        self.assertLessEqual(top+span,.60)  # waist, without legs
        self.assertAlmostEqual((box[2]-box[0])/(box[3]-box[1]),16/9,delta=.02)
        scene['framing']['movement']='zoom_in'
        self.assertLess(guides.shot_frame(scene,1)[3],guides.shot_frame(scene,0)[3])

    def test_new_agent_output_requires_framing_and_valid_source(self):
        for field,value in [('shot_size','anything'),('angle','unknown'),('reference_id','R999'),('source_scene',0)]:
            plan=motion(); plan['scenes'][0]['framing'][field]=value
            with self.assertRaises(ValueError): guides.validate(plan,{'R1'},require_framing=True)
        plan=motion(); del plan['scenes'][0]['framing']
        with self.assertRaises(ValueError): guides.validate(plan,{'R1'},require_framing=True)
        guides.validate(plan,{'R1'})  # existing saved plans remain readable

    def test_six_requests_overlap_and_partial_success_keeps_order(self):
        barrier = threading.Barrier(6)
        references = [{'reference_id':f'R{i}','scene_details':[{'scene_number':1}]} for i in range(1,7)]
        def generate(variant,*args,**kwargs):
            barrier.wait(timeout=5)
            if variant['index']==4:
                raise ValueError('secret failure')
            return {'state':'completed','index':variant['index'],'mode':variant['mode']}
        with tempfile.TemporaryDirectory() as folder, patch.object(guides,'VideoPool'), patch.object(guides,'generate_one',side_effect=generate) as request:
            result=guides.generate_videos({}, {}, references,folder)
        self.assertEqual(request.call_count,6)
        self.assertEqual(result['completed'],5)
        self.assertEqual(result['state'],'partial')
        self.assertEqual([v['index'] for v in result['videos']],list(range(1,7)))
        self.assertEqual([v['mode'] for v in result['videos']],['format']*3+['blend']*3)
        self.assertNotIn('secret failure',str(result))
        self.assertEqual(len(request.call_args_list[0].args[0]['references'][0]['scene_details']),1)

    def test_motion_validation_rejects_bad_timing_and_unknown_sources(self):
        self.assertEqual(guides.validate(motion(),{'R1'}),motion())
        for invalid in ('timing','position','reference'):
            value=motion()
            if invalid=='timing': value['scenes'][1]['start_second']=3
            if invalid=='position': value['scenes'][0]['actors'][0]['x']=float('nan')
            if invalid=='reference': value['reference_ids']=['R999']
            with self.assertRaises(ValueError): guides.validate(value,{'R1'})

    def test_incomplete_agent_can_only_render_a_complete_validated_timeline_with_warning(self):
        variant={'index':1,'mode':'format','reference_ids':['R1']}
        with tempfile.TemporaryDirectory() as folder, patch.object(guides,'render_video'):
            result=guides.render_response(json.dumps(motion()),'incomplete',variant,Path(folder),'a'*32)
            self.assertIn('warning',result)
            invalid=motion(); invalid['scenes'][-1]['end_second']=4
            with self.assertRaises(ValueError): guides.render_response(json.dumps(invalid),'incomplete',variant,Path(folder),'b'*32)

    def test_actual_encoder_produces_playable_mp4_with_multiple_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'sample.mp4'
            guides.render_video(guides.validate(motion(),{'R1'}),path)
            frames=guides.imageio_ffmpeg.read_frames(str(path))
            try:
                metadata=next(frames)
                self.assertEqual(metadata['size'],(guides.WIDTH,guides.HEIGHT))
                self.assertAlmostEqual(metadata['duration'],5,delta=.2)
                first=next(frames); later=next(frames)
                self.assertNotEqual(first,later)
            finally: frames.close()

    def test_agent_receives_scene_details_and_uses_its_own_pool(self):
        pool=Mock(); pool.keys=[('acct1','fake-key')]
        pool.acquire_blocking.return_value=SimpleNamespace(api_key='fake-key')
        reply=Mock(); reply.json.return_value={'status':'completed','steps':[{'type':'model_output','content':[{'type':'text','text':json.dumps(motion())}]}]}
        variant={'index':1,'mode':'format','references':[{'reference_id':'R1','analysis':{'summary':'요약'},'metrics':{'view_count':1234,'comment_count':0,'follower_count':1000},'scene_details':[{'camera':'full shot'}]}]}
        with tempfile.TemporaryDirectory() as folder, patch.object(guides,'VideoPool',return_value=pool) as factory, patch.object(guides.requests,'post',return_value=reply) as request, patch.object(guides,'render_video'):
            result=guides.generate_one(variant,{}, {},Path(folder))
        factory.assert_called_once_with(models=[guides.GUIDE_AGENT])
        self.assertIn('full shot',request.call_args.kwargs['json']['input'])
        request_data=json.loads(request.call_args.kwargs['json']['input'].split('\n')[-1])
        self.assertNotIn('plan',request_data)
        self.assertEqual(request_data['reference_data'][0]['metrics'],{'view_count':1234,'comment_count':0,'follower_count':1000})
        self.assertEqual(result['scenes'][0]['dialogue'],'이 제품을 소개합니다.')
        self.assertEqual(request.call_args.kwargs['json']['tools'],[])
        self.assertEqual(request.call_args.kwargs['json']['agent_config']['type'],'antigravity')
        self.assertEqual(result['state'],'completed')
        self.assertIsNone(pool.finish.call_args.kwargs['error'])

    def test_video_filename_rejects_path_traversal(self):
        service=recommendations.Recommendations()
        for filename in ('../.env','file.mp4','a'*32+'.png','a'*32+'.mp4/../x'):
            with self.assertRaises(ValueError): service.video(filename)

    def test_video_http_range_and_missing_files(self):
        from http.server import ThreadingHTTPServer
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'collectors'/'monitor'/'backend'))
        from collectors.monitor.backend.server import Handler
        class QuietHandler(Handler):
            def log_message(self,*args): pass
        with tempfile.TemporaryDirectory() as folder, patch.object(recommendations,'ANALYZER',Path(folder)):
            output=Path(folder)/'output'/'guides'; output.mkdir(parents=True)
            name='a'*32+'.mp4'; (output/name).write_bytes(b'0123456789')
            (output/('a'*32+'.video.json')).write_text(json.dumps({'metadata':{'filename':name,'state':'completed','scenes':[],'secret':'hidden'},'motion':{'private':'hidden'}}),encoding='utf-8')
            server=ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
            server.monitor=SimpleNamespace(recommendations=recommendations.Recommendations())
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            try:
                url=f'http://127.0.0.1:{server.server_port}/api/recommendation/video?filename='
                reply=guides.requests.get(url+name,headers={'Range':'bytes=2-5'},timeout=5)
                self.assertEqual(reply.status_code,206)
                self.assertEqual(reply.content,b'2345')
                self.assertEqual(reply.headers['Content-Range'],'bytes 2-5/10')
                self.assertEqual(guides.requests.get(url+name,headers={'Range':'bytes=20-'},timeout=5).status_code,416)
                self.assertEqual(guides.requests.get(url+'b'*32+'.mp4',timeout=5).status_code,400)
                metadata=guides.requests.get(url.replace('/video?','/video-plan?')+name,timeout=5).json()
                self.assertEqual(metadata['video']['filename'],name)
                self.assertNotIn('hidden',json.dumps(metadata))
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__=='__main__': unittest.main()
