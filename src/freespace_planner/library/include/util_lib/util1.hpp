//
// Created by schumann on 11/4/22.
//

#ifndef FREESPACE_PLANNER_UTIL1_HPP
#define FREESPACE_PLANNER_UTIL1_HPP

#include <cmath>
#include <vector>

namespace util
{
constexpr double PI = M_PI;
constexpr double TO_DEGREES = 180 / PI;
constexpr double TO_RAD = PI / 180;

double constrainAngleZero2Pi(double angle);

double constrainAngleMinPIPlusPi(double angle);

double getSignedAngleDiff(double angle1, double angle2);

double getAngleDiff(double angle1, double angle2);

double getAngleDiffInDir(double goal_angle, double start_angle, int dir);

template <typename T>
int sgn(T val)
{
  return (T(0) < val) - (val < T(0));
}

/**
 * extend a vector with another one
 * @tparam T
 * @param vec
 * @param v_add
 */
template <typename T>
void extend_vector(std::vector<T>& vec, std::vector<T>& v_add)
{
  vec.reserve(vec.size() + distance(v_add.begin(), v_add.end()));
  vec.insert(vec.end(), std::make_move_iterator(v_add.begin()), std::make_move_iterator(v_add.end()));
  //  vec.insert(vec.end(), v_add.begin(), v_add.end());
}

double bilinearInterpolation(double q11,
                             double q12,
                             double q21,
                             double q22,
                             int x1,
                             int x2,
                             int y1,
                             int y2,
                             double x,
                             double y);

template <typename T>
static std::vector<T> slice(std::vector<T> const& v, size_t start, size_t end)
{
  auto first = v.cbegin() + start;
  auto last = v.cbegin() + end + 1;

  std::vector<T> vec(first, last);
  return vec;
}

template <typename T>
static void extend(std::vector<T>& path, std::vector<T>& path_segment, bool inclusive_end = false)
{
  int idx_shift = 1;
  if (inclusive_end)
  {
    idx_shift = 0;
  }
  // reserve() is optional - just to improve performance
  path.reserve(path.size() + distance(path_segment.begin(), path_segment.end() - idx_shift));
  path.insert(path.end(), path_segment.begin(), path_segment.end() - idx_shift);
}

}  // namespace util

#endif  // FREESPACE_PLANNER_UTIL1_HPP
