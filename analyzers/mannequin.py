"""Small CPU mesh renderer for volumetric, articulated shooting mannequins."""
import math


def draw_solid_object(pen, origin, scale, kind, projector=None):
    """Render a solid camera or product with visible front, side and top faces."""
    yaw,pitch=-.55,-.25
    def project(p):
        if projector is not None:
            return projector(p)
        x,y,z=p
        x,z=x*math.cos(yaw)+z*math.sin(yaw),-x*math.sin(yaw)+z*math.cos(yaw)
        return (origin[0]+x*scale,origin[1]+(y*math.cos(pitch)-z*math.sin(pitch))*scale,z)
    faces=[]
    def box(center,size,color):
        vertices=[project((center[0]+x*size[0]/2,center[1]+y*size[1]/2,center[2]+z*size[2]/2))
            for x,y,z in ((-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1))]
        for ids,shade in (((0,1,2,3),.65),((4,7,6,5),1),((0,4,5,1),1.15),((3,2,6,7),.6),((0,3,7,4),.72),((1,5,6,2),.82)):
            pts=[vertices[i] for i in ids]
            faces.append((sum(p[2] for p in pts)/4,[p[:2] for p in pts],tuple(min(255,round(c*shade)) for c in color)))
    if kind in ('camera','camera_body'):
        if kind=='camera':
            for end in ((-.38,.85,-.25),(.38,.85,-.25),(0,.85,.35)):
                a,b=project((0,.05,0)),project(end)
                pen.line((a[:2],b[:2]),fill='#3c505d',width=5)
                pen.line(((a[0]-1,a[1]),(b[0]-1,b[1])),fill='#91a8b3',width=2)
        box((0,-.12,0),(.65,.40,.35),(87,116,133))
        box((0,-.12,.30),(.27,.26,.30),(44,65,81))
        box((-.16,-.36,0),(.22,.10,.22),(113,138,152))
    else:
        box((0,0,0),(.72,.9,.32),(105,176,172))
    for _,points,color in sorted(faces,key=lambda f:f[0]):
        pen.polygon(points,fill=color)


def draw_mannequin(pen, origin, scales, yaw, pitch, pose, progress, mirror=False):
    """Rotate solid meshes, retaining physical depth even in a side view."""
    faces=[]
    cloth=(92,124,147); skin=(218,187,153); trousers=(54,72,86)

    def transform(p):
        x,y,z=p
        x,z=x*math.cos(yaw)+z*math.sin(yaw),-x*math.sin(yaw)+z*math.cos(yaw)
        return x,y*math.cos(pitch)-z*math.sin(pitch),y*math.sin(pitch)+z*math.cos(pitch)

    def mesh(vertices, triangles, color, center):
        for ids in triangles:
            points=[vertices[i] for i in ids]
            a,b,c=points
            u=[b[i]-a[i] for i in range(3)]; v=[c[i]-a[i] for i in range(3)]
            normal=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
            middle=tuple(sum(p[i] for p in points)/3 for i in range(3))
            if sum(normal[i]*(middle[i]-center[i]) for i in range(3))<0:
                normal=tuple(-n for n in normal)
            normal=transform(normal)
            length=math.sqrt(sum(n*n for n in normal)) or 1
            light=max(0,sum(n*q for n,q in zip(normal,(-.45,-.55,.70)))/length)
            shade=.48+.54*light
            fill=tuple(min(255,round(c*shade+14*light**12)) for c in color)
            rotated=[transform(p) for p in points]
            projected=[(origin[0]+p[0]*scales[0],origin[1]+p[1]*scales[1]) for p in rotated]
            faces.append((sum(p[2] for p in rotated)/3,projected,fill))

    def ellipsoid(center, radii, color, direction=None):
        # Optional local Y axis aligns the volume between articulated joints.
        axis=direction or (0,1,0)
        length=math.sqrt(sum(v*v for v in axis)); axis=tuple(v/length for v in axis)
        seed=(1,0,0) if abs(axis[0])<.8 else (0,0,1)
        dot=sum(seed[i]*axis[i] for i in range(3))
        right=tuple(seed[i]-axis[i]*dot for i in range(3))
        length=math.sqrt(sum(v*v for v in right)); right=tuple(v/length for v in right)
        front=(right[1]*axis[2]-right[2]*axis[1],right[2]*axis[0]-right[0]*axis[2],right[0]*axis[1]-right[1]*axis[0])
        vertices=[]; triangles=[]; rings=8; segments=16
        for j in range(rings+1):
            latitude=math.pi*j/rings
            for k in range(segments):
                angle=2*math.pi*k/segments
                local=(radii[0]*math.sin(latitude)*math.cos(angle),radii[1]*math.cos(latitude),radii[2]*math.sin(latitude)*math.sin(angle))
                vertices.append(tuple(center[i]+right[i]*local[0]+axis[i]*local[1]+front[i]*local[2] for i in range(3)))
        for j in range(rings):
            for k in range(segments):
                a=j*segments+k; b=j*segments+(k+1)%segments
                triangles.extend(((a,b,a+segments),(b,b+segments,a+segments)))
        mesh(vertices,triangles,color,center)

    def limb(a,b,radius,color):
        direction=tuple(b[i]-a[i] for i in range(3))
        length=math.sqrt(sum(v*v for v in direction))
        ellipsoid(tuple((a[i]+b[i])/2 for i in range(3)),(radius,length/2+radius*.4,radius),color,direction)

    # Elliptical cross-sections define chest, waist and back, not a flat jacket.
    rings=[(.20,.065,.065),(.235,.145,.085),(.30,.143,.09),(.43,.12,.078),(.55,.125,.075)]
    vertices=[]; triangles=[]; segments=24
    for y,rx,rz in rings:
        for k in range(segments):
            angle=2*math.pi*k/segments
            vertices.append((rx*math.cos(angle),y,rz*math.sin(angle)))
    for j in range(len(rings)-1):
        for k in range(segments):
            a=j*segments+k; b=j*segments+(k+1)%segments
            triangles.extend(((a,b,a+segments),(b,b+segments,a+segments)))
    mesh(vertices,triangles,cloth,(0,.37,0))
    ellipsoid((0,.18,0),(.035,.05,.035),skin)
    ellipsoid((0,.085,0),(.063,.085,.065),skin)
    ellipsoid((0,.56,0),(.115,.055,.07),trousers)
    stride=math.sin(progress*math.pi*4) if pose['gesture']=='walk' else 0
    for side in (-1,1):
        hip=(side*.06,.56,0)
        knee=(side*.065,.77,side*stride*.055)
        ankle=(side*.075,.985,-side*stride*.045)
        limb(hip,knee,.044,trousers); limb(knee,ankle,.032,trousers)
        ellipsoid(knee,(.034,.036,.035),trousers)
        ellipsoid((ankle[0],1.005,ankle[2]+.025),(.037,.025,.075),(40,49,56))
        hand=pose['left'] if side<0 else pose['right']
        shoulder=(side*.145,.25,0)
        elbow=(side*.20,(.25+hand[1])/2+.025,.025)
        wrist=(hand[0],hand[1],.105 if hand[1]<.46 else .025)
        limb(shoulder,elbow,.043,cloth); limb(elbow,wrist,.033,cloth)
        ellipsoid(shoulder,(.045,.047,.047),cloth)
        ellipsoid(elbow,(.035,.035,.035),cloth)
        ellipsoid(wrist,(.023,.032,.022),skin)
    # The zipper is on the front surface; the depth sort hides it from the back.
    limb((0,.245,.089),(0,.53,.078),.0035,(201,209,211))
    for side in (-1,1):
        limb((side*.025,.225,.065),(side*.065,.27,.086),.011,(153,171,183))
    if mirror:
        x,y=pose['left']; z=.145
        vertices=[(x+dx,y+dy,z+dz) for dx,dy,dz in
            ((-.027,-.047,-.009),(.027,-.047,-.009),(.027,.047,-.009),(-.027,.047,-.009),
             (-.027,-.047,.009),(.027,-.047,.009),(.027,.047,.009),(-.027,.047,.009))]
        mesh(vertices,[(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),
            (1,5,6),(1,6,2),(2,6,7),(2,7,3),(3,7,4),(3,4,0)],(110,120,130),(x,y,z))
    for _,points,color in sorted(faces,key=lambda f:f[0]):
        pen.polygon(points,fill=color)
