"""A shared 3D scene for camera, subject, aim point and dashed view frustum."""
import math
from PIL import Image, ImageDraw
from mannequin import draw_mannequin, draw_solid_object
from scene_directions import AIM_HEIGHT, actor_yaw, camera_pitch


def unit(v):
    length=math.sqrt(sum(x*x for x in v)) or 1
    return tuple(x/length for x in v)


def cross(a,b):
    return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])


def camera_geometry(framing, top, span, subject_x=.65, progress=0):
    aim=AIM_HEIGHT.get(framing.get('aim_target'),framing.get('aim_height',max(.05,min(1.05,top+span/2))))
    target=(subject_x,aim,.1)
    height={'high_angle':-.65,'low_angle':.55,'overhead':-.95}.get(framing['angle'],0)
    camera=(-1.25,aim+height,1.0)
    if framing['angle']=='overhead': camera=(subject_x,aim+height,.1)
    elif framing['angle']=='side': camera=(subject_x-1.4,aim,.1)
    pitch=camera_pitch(framing)
    if pitch is not None:
        # Keep an illustrative distance, but preserve the supplied optical angle.
        dx,dz=(-1,0) if framing['angle']=='side' else (-1.9,.9)
        horizontal=math.hypot(dx,dz)
        radians=math.radians(pitch)
        distance=1.1
        camera=(subject_x+distance*math.cos(radians)*dx/horizontal,
            aim-distance*math.sin(radians),.1+distance*math.cos(radians)*dz/horizontal)
    if framing['movement'] in ('pan_left','pan_right'):
        camera=(camera[0]+(progress-.5)*(.5 if framing['movement']=='pan_left' else -.5),camera[1],camera[2])
    forward=unit(tuple(target[i]-camera[i] for i in range(3)))
    right=unit(cross((0,-1,0),forward))
    if abs(forward[1])>.99: right=(1,0,0)
    up=unit(cross(forward,right))
    ratio={'portrait':9/16,'square':1,'landscape':16/9}[framing['aspect']]
    corners=[tuple(target[i]+right[i]*sx*span*ratio/2+up[i]*sy*span/2 for i in range(3))
        for sx,sy in ((-1,-1),(1,-1),(1,1),(-1,1))]
    lens=tuple(camera[i]+forward[i]*.10 for i in range(3))
    return {'camera':camera,'target':target,'forward':forward,'right':right,'up':up,'corners':corners,'lens':lens}


def draw_camera_guide(image, scene, progress, font, framing, top, span, pose):
    layer=Image.new('RGB',(490,235),'#e8eff2'); pen=ImageDraw.Draw(layer)
    actors=scene['actors'] or [{}]
    positions=[.65+(actor.get('x',.5)+(actor.get('end_x',.5)-actor.get('x',.5))*progress-.5)*1.2 for actor in actors]
    subject_x=sum(positions)/len(positions)
    geometry=camera_geometry(framing,top,span,subject_x,progress)
    view_yaw=.45; view_pitch=.20
    def rotate(p):
        x,y,z=p
        x,z=x*math.cos(view_yaw)+z*math.sin(view_yaw),-x*math.sin(view_yaw)+z*math.cos(view_yaw)
        return (x,y*math.cos(view_pitch)-z*math.sin(view_pitch),z)
    floor_points=((-1.7,1.08,-.4),(1.6,1.08,-.4),(1.6,1.08,1.5),(-1.7,1.08,1.5))
    points=[rotate(p) for p in (*floor_points,geometry['camera'],*geometry['corners'],
        *((x,y,.1) for x in positions for y in (0,1.05)))]
    left,right=min(p[0] for p in points)-.1,max(p[0] for p in points)+.1
    high,low=min(p[1] for p in points)-.1,max(p[1] for p in points)+.1
    scale=min(125,460/(right-left),210/(low-high))
    origin=(245-(left+right)*scale/2,117-(high+low)*scale/2)
    def project(p):
        x,y,z=rotate(p)
        return (origin[0]+x*scale,origin[1]+y*scale,z)
    def dashed(a,b,color,width=1):
        a,b=project(a),project(b)
        distance=math.hypot(b[0]-a[0],b[1]-a[1])
        for start in range(0,max(1,math.ceil(distance)),10):
            t=start/max(1,distance); u=min(1,(start+5)/max(1,distance))
            pen.line((a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t,a[0]+(b[0]-a[0])*u,a[1]+(b[1]-a[1])*u),fill=color,width=width)
    # The floor, camera and mannequin use the same world-to-screen transform.
    floor=[project(p)[:2] for p in floor_points]
    pen.polygon(floor,fill='#d9e4e8')
    for x in (-1.5,-.75,0,.75,1.5): pen.line((project((x,1.08,-.4))[:2],project((x,1.08,1.5))[:2]),fill='#bdced5')
    for corner in geometry['corners']: dashed(geometry['lens'],corner,'#80b3bb')
    for i,corner in enumerate(geometry['corners']): dashed(corner,geometry['corners'][(i+1)%4],'#428e9c')
    if scene['actors']:
        for i,actor in enumerate(scene['actors']):
            origin=project((positions[i],0,.1))
            actor_pose=pose[i] if isinstance(pose,list) else pose
            draw_mannequin(pen,origin[:2],(scale,scale),math.radians(actor_yaw(actor))+view_yaw,view_pitch,actor_pose,progress)
    else:
        draw_solid_object(pen,project(geometry['target'])[:2],55,'product')
    dashed(geometry['lens'],geometry['target'],'#b77b28',2)
    target=project(geometry['target'])
    pen.ellipse((target[0]-4,target[1]-4,target[0]+4,target[1]+4),fill='#d68b29',outline='white')
    pen.text((target[0]+8,target[1]-9),'조준 지점',font=font,fill='#8c5a13')
    camera=geometry['camera']
    support=(camera[0]-.65,1.05,camera[2]+.35) if framing['angle']=='overhead' else (camera[0],1.05,camera[2])
    for dx,dz in ((-.18,-.12),(.18,-.12),(0,.18)):
        foot=(support[0]+dx,support[1],support[2]+dz)
        pen.line((project(camera)[:2],project(foot)[:2]),fill='#4e6979',width=3)
    def camera_project(p):
        # Camera local +Z is the optical axis and points exactly at the target.
        point=tuple(camera[i]+.22*(geometry['right'][i]*p[0]-geometry['up'][i]*(p[1]+.12)+geometry['forward'][i]*p[2]) for i in range(3))
        return project(point)
    draw_solid_object(pen,(0,0),1,'camera_body',projector=camera_project)
    image.paste(layer,(25,655))
    draw=ImageDraw.Draw(image)
    names={'high_angle':'위 → 아래','low_angle':'아래 → 위','eye_level':'대상 높이에서','side':'측면에서','overhead':'수직 위에서'}
    viewpoint=framing.get('camera_viewpoint')
    label=viewpoint if viewpoint and viewpoint!='확인 불가' else names[framing['angle']]
    draw.text((30,624),'3D 카메라 가이드 · '+label,font=font,fill='#117d88')
    draw.text((30,900),'점선: 시선·촬영 범위 / 거리·높이는 예시',font=font,fill='#446273')
    return geometry
